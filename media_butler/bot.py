"""Telegram bot: prompt, move, ignore, status and reboot."""

from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
import time
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from . import callbacks, organizer
from .archive import cleanup_incomplete, extraction_target, remove_extraction, volume_files
from .config import Config
from .db import Database, Item
from .emby import EmbyClient
from .scanner import Batch, Scanner
from .util import human_duration, human_size

log = logging.getLogger(__name__)

HELP_TEXT = (
    "🎛 Media Butler\n\n"
    "/scan — check the inbox now\n"
    "/status — queue, free space and status\n"
    "/reboot — restart the server (with confirmation)\n"
    "/help — this help\n\n"
    "When something new arrives I will ask whether it goes to Kids, Movies or Series."
)

MAX_NAME_IN_MESSAGE = 300


class MediaButlerBot:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.scanner = Scanner(config, db)
        self.emby = EmbyClient(config.emby)
        self.application: Application | None = None
        self.scan_lock = asyncio.Lock()
        self.move_lock = asyncio.Lock()
        self.reboot_task: asyncio.Task[None] | None = None
        self.reboot_token: str | None = None
        self.started_at = time.monotonic()
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------- lifecycle
    def build(self) -> Application:
        application = (
            Application.builder()
            .token(self.config.telegram.token)
            .post_init(self._on_startup)
            .post_shutdown(self._on_shutdown)
            .build()
        )
        application.add_handler(CommandHandler("start", self.cmd_start))
        application.add_handler(CommandHandler("help", self.cmd_help))
        application.add_handler(CommandHandler("scan", self.cmd_scan))
        application.add_handler(CommandHandler("status", self.cmd_status))
        application.add_handler(CommandHandler("reboot", self.cmd_reboot))
        application.add_handler(CallbackQueryHandler(self.on_callback))
        self.application = application
        return application

    def run(self) -> None:
        application = self.build()
        log.info(
            "starting: inbox=%s destinations=%s dry_run=%s",
            self.config.paths.inbox,
            {key: str(value) for key, value in self.config.paths.destinations.items()},
            self.config.behavior.dry_run,
        )
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )

    async def _on_startup(self, application: Application) -> None:
        self.application = application
        await self._set_commands(application)

        removed = await asyncio.to_thread(
            organizer.cleanup_temp_dirs,
            self.config.paths.inbox,
            *self.config.paths.destinations.values(),
        )
        repaired = self.db.reconcile_moving(
            delete_source=None if self.config.behavior.dry_run else organizer.remove_source
        )
        purged = self.db.purge_old(30)
        archives = await asyncio.to_thread(self._cleanup_finished_archives)
        await asyncio.to_thread(cleanup_incomplete, self.config.extract_dir)
        log.info(
            "maintenance: temp=%d reconciled=%s purged=%d archives=%d",
            removed,
            repaired,
            purged,
            archives,
        )

        boot_requested = self.db.state_get("boot_notify") == "1"
        self.db.state_delete("boot_notify")

        if not self.config.telegram.can_send:
            log.warning(
                "no allowed user in telegram.allowed_users; "
                "the bot will only reply with their ID to anyone sending /start"
            )
            return

        if boot_requested and self.config.reboot.notify_on_boot:
            await self.send("✅ Server is back online.")
        if any(repaired.values()):
            await self.send(
                "ℹ️ After the restart: "
                f"{repaired['done']} move(s) completed, "
                f"{repaired['pending']} returned to the queue, "
                f"{repaired['error']} failed."
            )

        await self._resend_missing()
        self._spawn(self._scan_loop(), "media-butler-scan")

    async def _on_shutdown(self, application: Application) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if self.reboot_task and not self.reboot_task.done():
            self.reboot_task.cancel()
            tasks.append(self.reboot_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)

        def _done(finished: asyncio.Task) -> None:
            if finished in self._tasks:
                self._tasks.remove(finished)

        task.add_done_callback(_done)
        return task

    async def _set_commands(self, application: Application) -> None:
        try:
            await application.bot.set_my_commands(
                [
                    BotCommand("scan", "Check the inbox now"),
                    BotCommand("status", "Status and free space"),
                    BotCommand("reboot", "Restart the server"),
                    BotCommand("help", "Help"),
                ]
            )
        except TelegramError as exc:
            log.warning("could not register the commands: %s", exc)

    # -------------------------------------------------------------- scanning
    async def _scan_loop(self) -> None:
        while True:
            try:
                await self.scan_and_notify()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("scan failed")
            await asyncio.sleep(self.config.scan.interval_seconds)

    async def scan_and_notify(self, force: set[str] | None = None) -> tuple[int, int]:
        if not self.config.telegram.can_send:
            return 0, 0
        async with self.scan_lock:
            await self._resend_missing()
            if self.db.awaiting_count() >= self.config.scan.max_pending:
                log.info("queue full (%d waiting), skipping scan", self.db.awaiting_count())
                return 0, 0

            batches = await asyncio.to_thread(self.scanner.scan_once, force)
            added = 0
            for batch in batches:
                if self.db.awaiting_count() >= self.config.scan.max_pending:
                    log.info("queue full, leaving batches unasked")
                    break
                item = self._register(batch)
                if item is None:
                    continue
                try:
                    message = await self.send(
                        self._prompt_text(item), self._prompt_keyboard(item)
                    )
                except TelegramError as exc:
                    log.warning("could not send the prompt for %s: %s", item.source, exc)
                    continue
                self.db.update(item.id, message_id=message.message_id)
                added += 1
                log.info("new batch %s: %s", item.id, item.source)
            return len(batches), added

    def _register(self, batch: Batch) -> Item | None:
        return self.db.add_item(
            item_id=secrets.token_hex(4),
            source=str(batch.source),
            kind=batch.kind,
            display_name=batch.display_name,
            size_bytes=batch.size_bytes,
            video_count=batch.video_count,
            fingerprint=batch.fingerprint,
            archive_path=str(batch.archive_path) if batch.archive_path else None,
            status="blocked" if batch.problem else "pending",
            error=batch.problem,
        )

    async def _resend_missing(self) -> None:
        for item in self.db.items_with_status("pending", "blocked"):
            if item.message_id is not None:
                continue
            try:
                message = await self.send(self._prompt_text(item), self._prompt_keyboard(item))
            except TelegramError as exc:
                log.warning("could not resend the prompt for %s: %s", item.source, exc)
                continue
            self.db.update(item.id, message_id=message.message_id)

    # ------------------------------------------------------------- rendering
    def _prompt_text(self, item: Item) -> str:
        name = item.display_name
        if len(name) > MAX_NAME_IN_MESSAGE:
            name = name[:MAX_NAME_IN_MESSAGE] + "…"
        if item.status == "blocked":
            return (
                f"⚠️ Could not process “{name}”\n"
                f"📦 {human_size(item.size_bytes)}\n\n"
                f"{item.error or 'unknown reason'}\n\n"
                "It stays in the inbox."
            )
        if item.kind == "video" or item.video_count <= 1:
            detail = human_size(item.size_bytes)
        else:
            plural = "videos" if item.video_count != 1 else "video"
            detail = f"{human_size(item.size_bytes)} · {item.video_count} {plural}"
        return f"🎬 {name}\n📦 {detail}\n\nWhere should I move it?"

    def _prompt_keyboard(self, item: Item) -> InlineKeyboardMarkup:
        if item.status == "blocked":
            return InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 Retry", callback_data=callbacks.encode_retry(item.id)
                        ),
                        InlineKeyboardButton(
                            "❌ Ignore", callback_data=callbacks.encode_ignore(item.id)
                        ),
                    ]
                ]
            )
        first_row = [
            InlineKeyboardButton(
                callbacks.DESTINATION_LABELS[destination],
                callback_data=callbacks.encode_move(item.id, destination),
            )
            for destination in callbacks.DESTINATIONS
        ]
        second_row = [
            InlineKeyboardButton("❌ Ignore", callback_data=callbacks.encode_ignore(item.id))
        ]
        return InlineKeyboardMarkup([first_row, second_row])

    # ------------------------------------------------------------- callbacks
    def _is_allowed(self, user) -> bool:  # noqa: ANN001 - telegram User | None
        return bool(user) and user.id in self.config.telegram.allowed_users

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._is_allowed(update.effective_user):
            await query.answer("Not authorized", show_alert=True)
            return
        await query.answer()
        data = query.data or ""

        move = callbacks.parse_move(data)
        if move is not None:
            await self._handle_move(query, *move)
            return

        ignore = callbacks.parse_ignore(data)
        if ignore is not None:
            await self._handle_ignore(query, ignore)
            return

        retry = callbacks.parse_retry(data)
        if retry is not None:
            await self._handle_retry(query, retry)
            return

        reboot = callbacks.parse_reboot(data)
        if reboot is not None:
            await self._handle_reboot_action(query, *reboot)

    async def _handle_move(self, query, item_id: str, destination: str) -> None:  # noqa: ANN001
        item = self.db.get(item_id)
        if item is None or item.status != "pending":
            await self._edit(query, "This batch is no longer pending.")
            return

        root = self.config.paths.destinations[destination]
        label = callbacks.DESTINATION_LABELS[destination]

        async with self.move_lock:
            # Re-read under the lock: a second click may have arrived while
            # another move was in progress, or the item may have been ignored.
            item = self.db.get(item_id)
            if item is None or item.status != "pending":
                await self._edit(query, "This batch is no longer pending.")
                return
            planned = organizer.plan_destination(Path(item.source), root, item.display_name)
            # Persist the destination before touching the disk: if the process
            # dies mid-move, startup reconciliation can tell where it went.
            self.db.set_status(
                item.id, "moving", dest=destination, final_path=str(planned), error=None
            )
            await self._edit(query, f"⏳ Moving “{item.display_name}” to {label}…")
            try:
                final = await asyncio.to_thread(
                    organizer.move_to_destination,
                    Path(item.source),
                    root,
                    item.display_name,
                    item.id,
                    dry_run=self.config.behavior.dry_run,
                    destination=planned,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("failed to move %s", item.source)
                self.db.set_status(item.id, "pending", dest=None, final_path=None, error=str(exc)[:500])
                failed = self.db.get(item.id) or item
                await self._edit(
                    query,
                    self._prompt_text(failed) + f"\n\n⚠️ Error: {str(exc)[:500]}",
                    self._prompt_keyboard(failed),
                )
                return

        if self.config.behavior.dry_run:
            self.db.set_status(item.id, "pending", dest=None, final_path=None)
            await self._edit(
                query,
                self._prompt_text(item) + f"\n\n🧪 dry-run: it would have gone to {final}",
                self._prompt_keyboard(item),
            )
            return

        self.db.set_status(item.id, "done", dest=destination, final_path=str(final), error=None)
        await self._edit(query, f"✅ “{item.display_name}” → {label}\n{final}")
        await self.emby.refresh()
        await asyncio.to_thread(self._cleanup_archive, item)

    async def _handle_ignore(self, query, item_id: str) -> None:  # noqa: ANN001
        item = self.db.get(item_id)
        if item is None or item.status not in ("pending", "blocked"):
            await self._edit(query, "This batch is no longer pending.")
            return
        self.db.set_status(item.id, "ignored", error=None)
        await self._edit(
            query,
            f"❌ “{item.display_name}” ignored. It stays in {self.config.paths.inbox}.",
        )
        if item.archive_path:
            # The archive itself is untouched, so the extracted copy is redundant.
            try:
                extracted, _ = extraction_target(Path(item.archive_path), self.config.extract_dir)
            except OSError as exc:
                log.warning("could not locate the extraction of %s: %s", item.archive_path, exc)
                return
            await asyncio.to_thread(remove_extraction, extracted, self.config.extract_dir)

    async def _handle_retry(self, query, item_id: str) -> None:  # noqa: ANN001
        item = self.db.get(item_id)
        if item is None or item.status != "blocked":
            await self._edit(query, "This batch is no longer blocked.")
            return
        if query.message is None:
            return
        self.db.delete(item.id)
        await self._edit(query, f"🔄 Retrying “{item.display_name}”…")
        self._spawn(
            self._retry_scan(
                query.message.chat_id,
                query.message.message_id,
                item.display_name,
                item.archive_path or item.source,
            ),
            "media-butler-retry",
        )

    async def _retry_scan(
        self, chat_id: int, message_id: int, name: str, source: str | None = None
    ) -> None:
        try:
            _, added = await self.scan_and_notify(force={source} if source else None)
        except Exception as exc:  # noqa: BLE001
            log.exception("retry failed for %s", name)
            await self._edit_ids(chat_id, message_id, f"❌ Retry failed: {str(exc)[:300]}")
            return
        if added:
            await self._edit_ids(
                chat_id, message_id, f"🔄 “{name}” retried: new prompt above."
            )
        else:
            await self._edit_ids(
                chat_id,
                message_id,
                f"🔄 “{name}” retried, but nothing new was found. Check the log.",
            )

    def _cleanup_archive(self, item: Item) -> None:
        if not item.archive_path:
            return
        archive_path = Path(item.archive_path)
        if self.config.behavior.dry_run:
            log.info("[dry-run] would have cleaned up %s", archive_path)
            return
        remove_extraction(Path(item.source), self.config.extract_dir)
        if not self.config.behavior.delete_archive:
            return
        if self.db.archive_has_open_items(str(archive_path)):
            return
        if self.db.archive_has_ignored_items(str(archive_path)):
            return
        for path in volume_files(archive_path):
            try:
                path.unlink()
                log.info("archive deleted: %s", path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("could not delete the archive %s: %s", path, exc)

    def _cleanup_finished_archives(self) -> int:
        """Delete archives whose move finished before the process crashed.

        ``_cleanup_archive`` normally runs right after a successful move; if
        the service dies in that window the archive would otherwise stay in
        the inbox forever, because its row is already ``done``.
        """
        removed = 0
        for item in self.db.items_with_status("done"):
            if not item.archive_path:
                continue
            archive_path = Path(item.archive_path)
            if not archive_path.exists():
                continue
            self._cleanup_archive(item)
            if not archive_path.exists():
                removed += 1
        return removed

    # ---------------------------------------------------------------- reboot
    async def _handle_reboot_action(self, query, token: str, action: str) -> None:  # noqa: ANN001
        stored = self.db.state_get("reboot_token") or ""
        stored_token, _, expires = stored.partition(":")
        valid = (
            self.reboot_token == token
            and stored_token == token
            and expires.isdigit()
            and int(expires) > int(time.time())
        )
        if not valid:
            await self._edit(query, "This confirmation has expired. Use /reboot again.")
            return

        if action == "no":
            self._cancel_reboot()
            self.db.state_delete("reboot_token")
            self.reboot_token = None
            await self._edit(query, "🛑 Reboot cancelled.")
            return

        self._cancel_reboot()
        assert query.message is not None
        chat_id = query.message.chat_id
        message_id = query.message.message_id
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Cancel", callback_data=callbacks.encode_reboot(token, "no"))]]
        )
        await self._edit(
            query,
            f"🔄 Rebooting in {self.config.reboot.countdown_seconds}s…",
            keyboard,
        )
        self.reboot_task = asyncio.create_task(
            self._reboot_countdown(chat_id, message_id, token),
            name="media-butler-reboot",
        )

    def _cancel_reboot(self) -> None:
        if self.reboot_task and not self.reboot_task.done():
            self.reboot_task.cancel()
        self.reboot_task = None

    async def _reboot_countdown(self, chat_id: int, message_id: int, token: str) -> None:
        try:
            await asyncio.sleep(self.config.reboot.countdown_seconds)
        except asyncio.CancelledError:
            return

        if self.reboot_token != token:
            return
        self.reboot_token = None
        self.db.state_delete("reboot_token")
        self.db.state_set("last_reboot_at", str(int(time.time())))
        if self.config.reboot.notify_on_boot:
            self.db.state_set("boot_notify", "1")

        await self._edit_ids(chat_id, message_id, "🔄 Rebooting the server now…")

        if self.config.behavior.dry_run:
            self.db.state_delete("boot_notify")
            log.info("[dry-run] not running systemctl reboot")
            await self.send("🧪 dry-run: nothing was rebooted.")
            return

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "--no-block", "reboot"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or f"systemctl exited with {result.returncode}")
        except Exception as exc:  # noqa: BLE001
            self.db.state_delete("boot_notify")
            log.exception("could not reboot the server")
            await self.send(f"⚠️ Could not reboot the server: {exc}")

    # -------------------------------------------------------------- commands
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if update.message is None or user is None:
            return
        chat = update.effective_chat
        group_hint = ""
        if chat is not None and chat.type in ("group", "supergroup"):
            group_hint = (
                f"\n\nThis chat_id is: {chat.id}\n"
                "Use it as telegram.chat_id to receive the prompts in this group."
            )
        if not self._is_allowed(user):
            log.warning("unauthorized user: %s (@%s)", user.id, user.username)
            await update.message.reply_text(
                "🔒 Not authorized.\n\n"
                f"Your Telegram user_id is: {user.id}\n\n"
                "Add it to telegram.allowed_users in /etc/media-butler/config.toml "
                "and restart the service." + group_hint
            )
            return
        await update.message.reply_text(HELP_TEXT + group_hint)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not self._is_allowed(update.effective_user):
            return
        await update.message.reply_text(HELP_TEXT)

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not self._is_allowed(update.effective_user):
            return
        message = await update.message.reply_text("🔍 Scanning the inbox…")
        self._spawn(self._manual_scan(message), "media-butler-manual-scan")

    async def _manual_scan(self, message: Message) -> None:
        if self.scan_lock.locked():
            await self._edit_ids(
                message.chat_id, message.message_id, "⏳ A scan is already running."
            )
            return
        awaiting = self.db.awaiting_count()
        if awaiting >= self.config.scan.max_pending:
            await self._edit_ids(
                message.chat_id,
                message.message_id,
                f"⏳ The queue is full ({awaiting} waiting, limit "
                f"{self.config.scan.max_pending}). Answer or ignore those prompts first.",
            )
            return
        try:
            total, added = await self.scan_and_notify()
        except Exception as exc:  # noqa: BLE001
            log.exception("manual scan failed")
            await self._edit_ids(message.chat_id, message.message_id, f"❌ Error: {exc}")
            return
        if total == 0:
            text = (
                "✅ Scan finished: nothing new. A file must be seen unchanged on two "
                f"scans at least {self.config.scan.settle_seconds}s apart, so wait "
                "and send /scan again."
            )
        elif added == 0:
            text = (
                f"✅ Scan finished: {total} batch(es) ready, but they are already "
                "waiting for an answer (see /status)."
            )
        else:
            text = f"✅ Scan finished: {total} batch(es) ready, {added} new prompt(s)."
        await self._edit_ids(message.chat_id, message.message_id, text)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not self._is_allowed(update.effective_user):
            return
        counts = self.db.counts()
        lines = [
            "📊 Media Butler",
            "",
            f"Pending: {counts.get('pending', 0)}",
            f"Moving: {counts.get('moving', 0)}",
            f"Blocked: {counts.get('blocked', 0)}",
            f"Failed: {counts.get('error', 0)}",
            f"Ignored: {counts.get('ignored', 0)}",
            "",
            f"Inbox: {self.config.paths.inbox}",
        ]
        for name, root in self.config.paths.destinations.items():
            try:
                free = organizer.usage(root)[2]
                lines.append(f"{callbacks.DESTINATION_LABELS[name]}: {human_size(free)} free")
            except OSError as exc:
                lines.append(f"{callbacks.DESTINATION_LABELS[name]}: ⚠️ {exc}")
        lines.extend(
            [
                "",
                f"Uptime: {human_duration(time.monotonic() - self.started_at)}",
                f"Mode: {'dry-run' if self.config.behavior.dry_run else 'normal'}",
                f"Emby: {'enabled' if self.config.emby.enabled else 'disabled'}",
            ]
        )
        await update.message.reply_text("\n".join(lines))

    async def cmd_reboot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not self._is_allowed(update.effective_user):
            return
        last = self.db.state_get("last_reboot_at")
        if last and last.isdigit():
            elapsed = time.time() - int(last)
            if elapsed < self.config.reboot.cooldown_seconds:
                remaining = self.config.reboot.cooldown_seconds - elapsed
                await update.message.reply_text(
                    f"⏳ Reboot cooldown. Try again in {human_duration(remaining)}."
                )
                return

        token = secrets.token_hex(4)
        self._cancel_reboot()
        self.reboot_token = token
        self.db.state_set("reboot_token", f"{token}:{int(time.time()) + 120}")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Confirm reboot",
                        callback_data=callbacks.encode_reboot(token, "yes"),
                    ),
                    InlineKeyboardButton(
                        "Cancel",
                        callback_data=callbacks.encode_reboot(token, "no"),
                    ),
                ]
            ]
        )
        await update.message.reply_text(
            "🔄 You are about to reboot the whole server.\n"
            f"It will run in {self.config.reboot.countdown_seconds}s and you can still cancel it.\n\n"
            "Confirm?",
            reply_markup=keyboard,
        )

    # ---------------------------------------------------------------- helpers
    async def send(self, text: str, keyboard: InlineKeyboardMarkup | None = None) -> Message:
        if self.application is None:
            raise RuntimeError("the application is not initialized")
        return await self.application.bot.send_message(
            chat_id=self.config.telegram.chat_id,
            text=text,
            reply_markup=keyboard,
        )

    async def _edit(self, query, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:  # noqa: ANN001
        if query.message is None:
            return
        await self._edit_ids(query.message.chat_id, query.message.message_id, text, keyboard)

    async def _edit_ids(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup | None = None,
    ) -> None:
        if self.application is None:
            return
        try:
            await self.application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=keyboard,
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("could not edit message %s: %s", message_id, exc)
        except TelegramError as exc:
            log.warning("could not edit message %s: %s", message_id, exc)
