"""
Phone Link (Enlace Móvil) notification reader.

Reads every notification your phone mirrors to Windows through Phone Link —
WhatsApp, Telegram, SMS, Facebook, Gmail, and anything else your phone forwards.

Two sources, tried in order:

  1. The Windows notification store (wpndatabase.db, SQLite).  Preferred, because
     each row carries the handler id that names the *phone* app the notification
     came from (`...!YourPhoneNotifications_com.whatsapp`), which the live API
     collapses to a single "Enlace Móvil" identity.  Needs nothing but stdlib.
  2. The WinRT UserNotificationListener, when the database can't be read.  Needs
     the `winrt-Windows.UI.Notifications.Management` package and the "notification
     access" privacy permission, so it is a fallback rather than the primary.

Both sources see the same notifications; only the metadata differs.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

PLUGIN = {
    "name": "phone_notifications",
    "description": (
        "READS AND DELETES Windows notifications, including everything the user's phone "
        "mirrors through Phone Link (Enlace Móvil): WhatsApp, Telegram, SMS, Facebook, Gmail "
        "and any other phone app. You ARE able to delete notifications — always use this tool "
        "instead of saying you cannot. "
        "action='read' (default) for 'what notifications do I have', 'read my notifications', "
        "'any new messages', 'did anyone text me', '¿qué notificaciones tengo?', 'léeme las "
        "notificaciones'. "
        "action='clear' to delete/dismiss them, for 'borra las notificaciones', 'elimina las "
        "notificaciones', 'limpia las notificaciones', 'quita las notificaciones', 'bórralas "
        "todas', 'clear all notifications', 'dismiss the WhatsApp ones'. Add scope='all' when "
        "the user means every notification on the PC and not just the phone's. Clearing is "
        "permanent, so only do it when the user actually asks — but when they do ask, just do "
        "it, no confirmation needed. "
        "action='watch' starts announcing new notifications out loud as they arrive, "
        "action='stop' ends that, action='apps' lists which apps have sent notifications. "
        "To send a message use send_message instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "read (default) | clear | watch | stop | apps",
            },
            "scope": {
                "type": "STRING",
                "description": (
                    "Which notifications an action covers: 'phone' (default) for the ones "
                    "mirrored from the phone, or 'all' for every notification in the Windows "
                    "notification centre, PC apps included. Use 'all' when the user says "
                    "'clear everything' or 'clear all notifications'."
                ),
            },
            "app": {
                "type": "STRING",
                "description": (
                    "Only notifications from this phone app, e.g. 'whatsapp', 'telegram', "
                    "'sms', 'facebook', 'gmail'. Omit for all apps."
                ),
            },
            "sender": {
                "type": "STRING",
                "description": "Only notifications whose sender/title contains this text.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "How many notifications to read back, newest first (default 10).",
            },
            "since_minutes": {
                "type": "INTEGER",
                "description": "Only notifications from the last N minutes. Omit for all.",
            },
        },
        "required": [],
    },
}

# ── Identity of Phone Link inside the Windows notification store ──────────────
# `Microsoft.YourPhone_*` is Phone Link itself; `MicrosoftWindows.CrossDevice_*`
# is the newer Windows 11 cross-device component that delivers the same mirrored
# notifications on some builds.
_PHONE_PREFIXES = ("microsoft.yourphone_", "microsoftwindows.crossdevice_")

_NOTIF_DIR = ("Microsoft", "Windows", "Notifications")
_DB_NAME = "wpndatabase.db"

# FILETIME epoch: 100-nanosecond ticks since 1601-01-01 UTC.
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

# Android package -> the name a person would actually say.
_APP_NAMES = {
    "com.whatsapp": "WhatsApp",
    "com.whatsapp.w4b": "WhatsApp Business",
    "org.telegram.messenger": "Telegram",
    "com.facebook.katana": "Facebook",
    "com.facebook.orca": "Messenger",
    "com.instagram.android": "Instagram",
    "com.google.android.gm": "Gmail",
    "com.google.android.apps.messaging": "Messages",
    "com.samsung.android.messaging": "Messages",
    "com.microsoft.office.outlook": "Outlook",
    "com.twitter.android": "X",
    "com.snapchat.android": "Snapchat",
    "com.spotify.music": "Spotify",
    "com.discord": "Discord",
    "com.linkedin.android": "LinkedIn",
    "com.zhiliaoapp.musically": "TikTok",
    "com.ss.android.ugc.trill": "TikTok",
    "com.slack": "Slack",
    "com.google.android.youtube": "YouTube",
    "com.google.android.dialer": "Phone",
    "com.android.server.telecom": "Phone",
    "com.openai.chatgpt": "ChatGPT",
    "com.microsoft.appmanager": "Phone Link",
}

# Package segments that carry no brand ("android", "ui", "ota"...). Dropping them
# stops `com.motorola.ccc.ota` from being announced as "Ota".
_NOISE_SEGMENTS = {
    "com", "org", "net", "io", "co", "android", "app", "apps", "mobile", "client",
    "ui", "ota", "ccc", "main", "free", "pro", "release", "prod",
}

_SMS_LABEL = "SMS"
_PHONE_LINK_LABEL = "Phone Link"


# ── Small helpers ─────────────────────────────────────────────────────────────

def _filetime_to_local(ticks: int) -> datetime | None:
    """FILETIME is UTC; return it in the machine's local zone (the whole point —
    reading it as local would put every notification hours off)."""
    try:
        return (_FILETIME_EPOCH + timedelta(microseconds=int(ticks) // 10)).astimezone()
    except Exception:
        return None


def _clean(text: str) -> str:
    """Toast payloads are indented XML, so text nodes arrive padded with newlines,
    tabs and non-breaking spaces. Collapse all of it."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _pretty_app(package: str) -> str:
    """A friendly name for an Android package we don't have mapped. The brand is
    normally the first segment that isn't boilerplate — `com.motorola.ccc.ota`
    is Motorola's, not "Ota"."""
    known = _APP_NAMES.get(package.lower())
    if known:
        return known
    segments = [s for s in package.lower().split(".") if s and s not in _NOISE_SEGMENTS]
    return segments[0].capitalize() if segments else "Phone"


def _parse_handler(primary_id: str) -> tuple[str, str]:
    """Split a Phone Link handler id into (kind, android package).

    `...!YourPhoneNotifications_com.whatsapp` -> ("app", "com.whatsapp")
    `...!YourPhoneMessages_<device guid>`     -> ("sms", "")
    `...!App`                                 -> ("phonelink", "")
    """
    suffix = primary_id.split("!", 1)[1] if "!" in primary_id else primary_id
    if suffix.startswith("YourPhoneNotifications_"):
        return "app", suffix[len("YourPhoneNotifications_"):]
    if suffix.startswith("YourPhoneMessages_"):
        return "sms", ""
    return "phonelink", ""


def _is_phone_link(primary_id: str) -> bool:
    return primary_id.lower().startswith(_PHONE_PREFIXES)


def _split_texts(texts: list[str], first_is_app: bool) -> tuple[str, str, str]:
    """Turn a toast's text nodes into (app display name, sender, body).

    Layout depends on the handler: a mirrored app notification leads with the
    app's own display name ('WhatsApp', 'Abuela Lunita', body), while an SMS
    leads straight with the sender ('Sid Lqm', body). That leading node is the
    name Phone Link itself shows, so it beats anything derived from the package.
    """
    parts = [t for t in (_clean(t) for t in texts) if t]
    app = ""
    if first_is_app and parts:
        app, parts = parts[0], parts[1:]
    if not parts:
        return app, "", ""
    if len(parts) == 1:
        return app, "", parts[0]
    return app, parts[0], " ".join(parts[1:])


def _parse_toast(payload: bytes) -> list[str]:
    """Text nodes of a toast payload, in `id` order."""
    try:
        root = ET.fromstring(bytes(payload).decode("utf-8", "replace"))
    except Exception:
        return []
    nodes = []
    for el in root.iter("text"):
        try:
            order = int(el.get("id") or 0)
        except ValueError:
            order = 0
        nodes.append((order, el.text or ""))
    return [t for _, t in sorted(nodes, key=lambda p: p[0])]


# ── Source 1: the Windows notification store ──────────────────────────────────

def _read_db(limit: int) -> list[dict]:
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), *_NOTIF_DIR)
    src = os.path.join(base, _DB_NAME)
    if not os.path.exists(src):
        raise FileNotFoundError("notification database not found")

    # The live database is held open by Windows and most of the recent rows sit
    # in the write-ahead log, so copy the whole set and read the copy. Copying
    # the .db alone silently loses the newest notifications.
    tmpdir = tempfile.mkdtemp(prefix="lumina_wpn_")
    try:
        for suffix in ("", "-wal", "-shm"):
            part = src + suffix
            if os.path.exists(part):
                shutil.copy2(part, os.path.join(tmpdir, _DB_NAME + suffix))

        conn = sqlite3.connect(os.path.join(tmpdir, _DB_NAME))
        try:
            rows = conn.execute(
                "SELECT n.Id, h.PrimaryId, n.Payload, n.ArrivalTime "
                "FROM Notification n "
                "JOIN NotificationHandler h ON h.RecordId = n.HandlerId "
                "WHERE n.Type = 'toast' "
                "ORDER BY n.ArrivalTime DESC LIMIT ?",
                (max(limit, 1) * 8,),
            ).fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    out = []
    for notif_id, primary_id, payload, arrival in rows:
        if not _is_phone_link(primary_id or ""):
            continue
        kind, package = _parse_handler(primary_id)
        display, sender, body = _split_texts(_parse_toast(payload or b""),
                                             first_is_app=(kind == "app"))
        if not (sender or body):
            continue
        if kind == "app":
            app = display or _pretty_app(package)
        elif kind == "sms":
            app = _SMS_LABEL
        else:
            app = _PHONE_LINK_LABEL
        out.append({
            "id": notif_id,
            "app": app,
            "package": package,
            "sender": sender,
            "body": body,
            "when": _filetime_to_local(arrival),
            "phone": True,
        })
    return out


# ── Source 2: the live WinRT listener ─────────────────────────────────────────

def _listener():
    """The live listener, or a PermissionError explaining how to switch it on.
    Deleting a notification is only possible through this API — the store on disk
    is a cache the shell does not re-read."""
    from winrt.windows.ui.notifications.management import (
        UserNotificationListener,
        UserNotificationListenerAccessStatus,
    )

    listener = UserNotificationListener.current
    if listener.get_access_status() != UserNotificationListenerAccessStatus.ALLOWED:
        raise PermissionError(
            "notification access is off — turn it on in Settings > Privacy > Notifications"
        )
    return listener


def _fetch_live():
    import asyncio

    from winrt.windows.ui.notifications import NotificationKinds

    listener = _listener()

    async def _go():
        return await listener.get_notifications_async(NotificationKinds.TOAST)

    return asyncio.run(_go())


def _texts_of(note) -> list[str]:
    """Text nodes of a live notification, or [] if it carries none."""
    try:
        binding = note.notification.visual.get_binding("ToastGeneric")
        return [t.text for t in binding.get_text_elements()] if binding else []
    except Exception:
        return []


def _read_listener(limit: int, phone_only: bool = True) -> list[dict]:
    out = []
    for note in _fetch_live():
        try:
            aumid = note.app_info.app_user_model_id or ""
        except Exception:
            aumid = ""
        phone = _is_phone_link(aumid)
        if phone_only and not phone:
            continue
        if not phone:
            # A PC app: Windows already knows its display name, no guessing needed.
            try:
                pc_app = note.app_info.display_info.display_name or "Windows"
            except Exception:
                pc_app = "Windows"
            parts = [t for t in (_clean(t) for t in _texts_of(note)) if t]
            sender = parts[0] if len(parts) > 1 else ""
            body = " ".join(parts[1:]) if len(parts) > 1 else (parts[0] if parts else "")
            if not (sender or body):
                continue
            when = None
            try:
                when = note.creation_time.astimezone()
            except Exception:
                pass
            out.append({"id": note.id, "app": pc_app, "package": "", "sender": sender,
                        "body": body, "when": when, "phone": False})
            continue
        texts = _texts_of(note)
        # The live API reports one identity for all of Phone Link, so there is no
        # handler to say whether the first line is an app name or a sender. Three
        # or more text nodes means app/sender/body; two means an SMS-shaped
        # sender/body pair.
        parts = [t for t in (_clean(t) for t in texts) if t]
        display, sender, body = _split_texts(texts, first_is_app=len(parts) >= 3)
        if not (sender or body):
            continue
        when = None
        try:
            when = note.creation_time.astimezone()
        except Exception:
            pass
        out.append({
            "id": note.id,
            "app": display or _PHONE_LINK_LABEL,
            "package": "",
            "sender": sender,
            "body": body,
            "when": when,
            "phone": True,
        })
    return out


def _collect(limit: int = 10) -> tuple[list[dict], str]:
    """Newest-first notifications plus the name of the source that produced them."""
    try:
        return _read_db(limit), "store"
    except Exception:
        items = _read_listener(limit)
        items.sort(key=lambda i: i["when"] or datetime.min.replace(tzinfo=timezone.utc),
                   reverse=True)
        return items, "listener"


# ── Filtering and phrasing ────────────────────────────────────────────────────

def _matches_app(item: dict, wanted: str) -> bool:
    wanted = wanted.strip().casefold()
    if not wanted:
        return True
    app = item["app"].casefold()
    if wanted in app or app in wanted:
        return True
    # Match the package too, so "whatsapp" still finds com.whatsapp even when the
    # toast displayed something else ("WhatsApp Business", a localised name...).
    if wanted in item.get("package", "").casefold():
        return True
    # "sms", "texts" and "messages" all mean the same thing to a person.
    if wanted in {"sms", "text", "texts", "message", "messages", "mensajes", "texto"}:
        return item["app"] == _SMS_LABEL
    return False


def _apply_filters(items: list[dict], app: str, sender: str, since_minutes: int) -> list[dict]:
    """Shared by reading and clearing, so 'clear the WhatsApp ones' removes exactly
    the set that 'read the WhatsApp ones' would have listed."""
    out = [i for i in items if _matches_app(i, app)]
    if sender:
        out = [i for i in out if sender in i["sender"].casefold()]
    if since_minutes > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        out = [i for i in out if i["when"] and i["when"].astimezone(timezone.utc) >= cutoff]
    return out


def _ago(when: datetime | None) -> str:
    if not when:
        return "recently"
    seconds = (datetime.now(timezone.utc) - when.astimezone(timezone.utc)).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    days = int(seconds // 86400)
    if days == 1:
        return f"yesterday at {when:%H:%M}"
    return f"{days} days ago ({when:%d %b %H:%M})"


def _describe(item: dict, with_time: bool = True) -> str:
    who = f"{item['app']} — {item['sender']}" if item["sender"] else item["app"]
    line = f"{who}: {item['body']}" if item["body"] else who
    return f"{line} ({_ago(item['when'])})" if with_time else line


# ── Deleting ──────────────────────────────────────────────────────────────────

def _remove_ids(ids: list[int]) -> int:
    """Dismiss notifications by id, counting only the ones Windows accepted. An
    id can already be gone — the user may have swiped it away while we were
    reading — and that must not abort the rest of the batch."""
    listener = _listener()
    removed = 0
    for notif_id in ids:
        try:
            listener.remove_notification(notif_id)
            removed += 1
        except Exception:
            continue
    return removed


def _summarise(items: list[dict]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        counts[item["app"]] = counts.get(item["app"], 0) + 1
    return ", ".join(f"{n} from {app}" for app, n in
                     sorted(counts.items(), key=lambda p: -p[1]))


def _do_clear(parameters: dict, player) -> str:
    scope = str(parameters.get("scope") or "phone").strip().casefold()
    scope_given = bool(str(parameters.get("scope") or "").strip())
    everything = scope in {"all", "everything", "todas", "todo", "todas las notificaciones"}
    app_filter = str(parameters.get("app") or "").strip()
    sender_filter = str(parameters.get("sender") or "").strip().casefold()
    since_minutes = _as_int(parameters.get("since_minutes"), 0)

    try:
        if everything:
            items = _read_listener(200, phone_only=False)
        else:
            items, _ = _collect(200)
    except PermissionError as e:
        return (f"I cannot clear your notifications: {e}. Reading them still works, "
                "but dismissing one needs that permission.")
    except Exception as e:
        return f"Sir, I could not reach the notifications to clear them: {e}."

    targets = _apply_filters(items, app_filter, sender_filter, since_minutes)

    # Nothing on the phone matched. The user is looking at one notification centre
    # that mixes phone and PC — "clear the Slack ones", or just "clear the
    # notifications" — so widen instead of dead-ending on a scope they never
    # chose. This can only ever fire when the phone set was already empty, so it
    # can never delete PC notifications in place of the phone ones.
    widened = False
    if not targets and not everything and (app_filter or sender_filter or not scope_given):
        try:
            targets = _apply_filters(_read_listener(200, phone_only=False),
                                     app_filter, sender_filter, since_minutes)
            widened = bool(targets)
        except Exception:
            targets = []

    if not targets:
        what = f" from {app_filter}" if app_filter else ""
        where = "" if everything else " on your phone"
        return f"There are no notifications{what}{where} to clear."

    try:
        # Always one id at a time. The listener's own clear_notifications() looks
        # like the right call for "clear everything", but it raises
        # [WinError -2147023728] Element not found and clears nothing, while
        # removing each id in turn works on the same machine.
        removed = _remove_ids([i["id"] for i in targets])
    except PermissionError as e:
        return f"I cannot clear your notifications: {e}."
    except Exception as e:
        return f"Sir, clearing the notifications failed: {e}."

    if removed == 0:
        return "Those notifications were already gone — nothing left to clear."

    detail = _summarise(targets[:removed]) if removed < len(targets) else _summarise(targets)
    _log(player, f"LUMINA: cleared {removed} notification(s) — {detail}.")
    scope_text = "" if (everything or widened) else " from your phone"
    note = " Those were PC notifications, not phone ones." if widened else ""
    return (f"Cleared {removed} notification{'s' if removed != 1 else ''}{scope_text} "
            f"({detail}). They are gone from the notification centre.{note}")


# ── Live announcing ───────────────────────────────────────────────────────────

_watch_lock = threading.Lock()
_watch_thread: threading.Thread | None = None
_watch_stop = threading.Event()
_POLL_SECONDS = 8
_MAX_ANNOUNCED_AT_ONCE = 3


def _watch_loop(player) -> None:
    # Prime with what is already there, so starting the watch never replays the
    # backlog — only genuinely new notifications get announced.
    try:
        seen = {i["id"] for i in _collect(200)[0]}
    except Exception:
        seen = set()

    while not _watch_stop.wait(_POLL_SECONDS):
        try:
            items, _ = _collect(50)
        except Exception:
            continue
        fresh = [i for i in items if i["id"] not in seen]
        seen.update(i["id"] for i in items)
        if not fresh:
            continue

        fresh.reverse()  # oldest first, so a burst is announced in order
        extra = len(fresh) - _MAX_ANNOUNCED_AT_ONCE
        for item in fresh[:_MAX_ANNOUNCED_AT_ONCE]:
            _log(player, f"LUMINA: phone notification — {_describe(item, with_time=False)}")
            _say(player, "A new phone notification just arrived. Tell the user about it "
                         "briefly and naturally, in their language: "
                         + _describe(item, with_time=False))
        if extra > 0:
            _say(player, f"Also mention that {extra} more phone notifications arrived "
                         "at the same time.")


def _start_watch(player) -> str:
    global _watch_thread
    with _watch_lock:
        if _watch_thread and _watch_thread.is_alive():
            return "I am already announcing your phone notifications as they arrive."
        _watch_stop.clear()
        _watch_thread = threading.Thread(
            target=_watch_loop, args=(player,), name="phone-notifications-watch", daemon=True
        )
        _watch_thread.start()
    return ("From now on I will announce your phone notifications the moment they arrive. "
            "Tell me to stop watching notifications when you want quiet.")


def _stop_watch() -> str:
    global _watch_thread
    with _watch_lock:
        if not (_watch_thread and _watch_thread.is_alive()):
            return "I was not announcing your phone notifications."
        _watch_stop.set()
        _watch_thread = None
    return "I will stop announcing phone notifications."


# ── UI plumbing (every hop is optional — the plugin must never crash on it) ───

def _log(player, message: str) -> None:
    try:
        if player:
            player.write_log(message)
    except Exception:
        pass


def _say(player, instruction: str) -> None:
    try:
        say = getattr(player, "request_say", None)
        if callable(say):
            say(instruction)
    except Exception:
        pass


def _show(player, title: str, text: str) -> None:
    try:
        if player:
            player.show_content(title, text)
    except Exception:
        pass


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Entry point ───────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "read").strip().lower()

    if action in {"watch", "start", "monitor", "vigilar", "avisar", "avisame"}:
        result = _start_watch(player)
        _log(player, f"LUMINA: {result}")
        return result

    if action in {"stop", "unwatch", "parar", "detener", "para"}:
        result = _stop_watch()
        _log(player, f"LUMINA: {result}")
        return result

    # Accept the Spanish verbs too: the model tends to echo the user's own wording
    # back into the action argument, and falling through to a silent read would
    # look exactly like a refusal to delete.
    if action in {"clear", "delete", "dismiss", "remove", "borrar", "borra", "eliminar",
                  "elimina", "limpiar", "limpia", "quitar", "quita"}:
        return _do_clear(parameters, player)

    limit = max(1, min(_as_int(parameters.get("limit"), 10), 50))
    app_filter = str(parameters.get("app") or "").strip()
    sender_filter = str(parameters.get("sender") or "").strip().casefold()
    since_minutes = _as_int(parameters.get("since_minutes"), 0)

    try:
        items, source = _collect(limit if action != "apps" else 200)
    except PermissionError as e:
        return f"I cannot read your phone notifications: {e}."
    except Exception as e:
        return (f"Sir, I could not read the phone notifications: {e}. "
                "Check that Phone Link is installed and linked to your phone.")

    if not items:
        return ("Phone Link has not delivered any notifications — there is nothing to read. "
                "If your phone should be sending them, check that notification mirroring "
                "is enabled in Phone Link.")

    if action == "apps":
        counts: dict[str, int] = {}
        for item in items:
            counts[item["app"]] = counts.get(item["app"], 0) + 1
        listing = ", ".join(f"{app} ({n})" for app, n in
                            sorted(counts.items(), key=lambda p: -p[1]))
        return f"Your phone is sending notifications from: {listing}."

    filtered = _apply_filters(items, app_filter, sender_filter, since_minutes)

    if not filtered:
        what = f" from {app_filter}" if app_filter else ""
        window = f" in the last {since_minutes} minutes" if since_minutes > 0 else ""
        return f"You have no phone notifications{what}{window}."

    shown = filtered[:limit]

    # Put the full list on screen while only the summary gets spoken — the same
    # split the news and search tools use.
    _show(player, "PHONE NOTIFICATIONS",
          "\n\n".join(f"{i+1}. {_describe(item)}" for i, item in enumerate(shown)))
    _log(player, f"LUMINA: read {len(shown)} phone notification(s) via {source}.")

    header = (f"{len(shown)} phone notification{'s' if len(shown) != 1 else ''}"
              f"{' from ' + app_filter if app_filter else ''}, newest first")
    body = "\n".join(f"- {_describe(item)}" for item in shown)
    more = ""
    if len(filtered) > len(shown):
        more = f"\n({len(filtered) - len(shown)} older ones not listed.)"
    return f"{header}:\n{body}{more}"
