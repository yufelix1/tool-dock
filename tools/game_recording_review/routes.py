import json
import logging
import os
import secrets
import tempfile
from collections import OrderedDict
from datetime import datetime, timezone
from threading import Lock

from flask import Blueprint, jsonify, render_template, request, send_from_directory


game_recording_review_bp = Blueprint(
    "game_recording_review",
    __name__,
    template_folder="../../templates/game_recording_review",
)

VIDEO_EXTENSIONS = {".mp4"}
COVER_EXTENSIONS = (".jpeg", ".jpg", ".png", ".webp")
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | set(COVER_EXTENSIONS)
MAX_SCAN_SESSIONS = 32
MAX_BATCH_RECORDINGS = 1000
MAX_NOTE_LENGTH = 500
FAVORITES_FILENAME = ".game-recording-review.json"
FAVORITES_VERSION = 1
SETTINGS_FILENAME = "game-recording-review.json"
SETTINGS_VERSION = 1
SETTINGS_CONFIG_DIR_ENV = "GAME_RECORDING_REVIEW_CONFIG_DIR"
DEFAULT_SETTINGS_CONFIG_DIR = "/config"

_scan_roots = OrderedDict()
_scan_roots_lock = Lock()
_favorites_lock = Lock()
_settings_lock = Lock()
logger = logging.getLogger(__name__)


def validate_root(root_path):
    roots, validation_error = validate_roots([root_path])
    return (roots[0], None) if not validation_error else (None, validation_error)


def validate_roots(root_paths):
    if not isinstance(root_paths, list) or not root_paths:
        return None, "请至少输入一个游戏根目录"

    normalized_roots = []
    seen = set()
    for root_path in root_paths:
        if not isinstance(root_path, str) or not root_path.strip():
            return None, "游戏根目录格式无效"

        normalized_root = os.path.realpath(os.path.expanduser(root_path.strip()))
        if not os.path.isdir(normalized_root):
            return None, f"路径不存在或不是目录：{normalized_root}"
        if normalized_root not in seen:
            seen.add(normalized_root)
            normalized_roots.append(normalized_root)

    return normalized_roots, None


def _settings_path():
    config_directory = os.environ.get(
        SETTINGS_CONFIG_DIR_ENV,
        DEFAULT_SETTINGS_CONFIG_DIR,
    ).strip()
    if not config_directory:
        config_directory = DEFAULT_SETTINGS_CONFIG_DIR
    config_directory = os.path.realpath(os.path.expanduser(config_directory))
    return os.path.join(config_directory, SETTINGS_FILENAME)


def _parse_settings_payload(payload):
    if not isinstance(payload, dict) or payload.get("version") != SETTINGS_VERSION:
        raise ValueError("设置文件版本无效")

    roots = payload.get("roots")
    ignored_directories = payload.get("ignored_directories")
    if not isinstance(roots, list) or not isinstance(ignored_directories, list):
        raise ValueError("设置文件格式无效")
    if any(not isinstance(path, str) or not path.strip() for path in roots):
        raise ValueError("设置文件格式无效")
    if any(
        not isinstance(path, str) or not path.strip()
        for path in ignored_directories
    ):
        raise ValueError("设置文件格式无效")

    return {
        "roots": roots,
        "ignored_directories": ignored_directories,
    }


def read_settings():
    settings_path = _settings_path()
    with _settings_lock:
        if os.path.islink(settings_path):
            raise ValueError("设置文件不能是符号链接")
        try:
            with open(settings_path, encoding="utf-8") as settings_file:
                payload = json.load(settings_file)
        except FileNotFoundError:
            return {"roots": [], "ignored_directories": []}
        except json.JSONDecodeError as error:
            raise ValueError("设置文件 JSON 格式无效") from error

    return _parse_settings_payload(payload)


def write_settings(root_paths, ignored_directories=None):
    normalized_roots, validation_error = validate_roots(root_paths)
    if validation_error:
        raise ValueError(validation_error)
    normalized_ignored_directories = normalize_ignored_directories(
        normalized_roots,
        ignored_directories,
    )
    payload = {
        "version": SETTINGS_VERSION,
        "roots": normalized_roots,
        "ignored_directories": normalized_ignored_directories,
    }

    settings_path = _settings_path()
    config_directory = os.path.dirname(settings_path)
    temporary_path = None
    file_descriptor = None
    with _settings_lock:
        os.makedirs(config_directory, exist_ok=True)
        if os.path.islink(settings_path):
            raise ValueError("设置文件不能是符号链接")
        try:
            file_descriptor, temporary_path = tempfile.mkstemp(
                dir=config_directory,
                prefix=f"{SETTINGS_FILENAME}.",
                suffix=".tmp",
            )
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as settings_file:
                file_descriptor = None
                json.dump(
                    payload,
                    settings_file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                settings_file.write("\n")
                settings_file.flush()
                os.fsync(settings_file.fileno())
            os.replace(temporary_path, settings_path)
            temporary_path = None
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if temporary_path is not None:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass

    return {
        "roots": normalized_roots,
        "ignored_directories": normalized_ignored_directories,
    }


def _record_error(errors, path, error):
    errors.append(f"{path}：{getattr(error, 'strerror', None) or str(error)}")


def _recording_created_at(stat):
    birth_time = getattr(stat, "st_birthtime", None)
    if birth_time is not None:
        return birth_time
    if os.name == "nt":
        return stat.st_ctime
    # Linux does not expose file birth time through os.stat; mtime is the
    # stable recording-time fallback and avoids using mutable inode ctime.
    return stat.st_mtime


def _directory_entries(path, errors):
    try:
        with os.scandir(path) as entries:
            return sorted(entries, key=lambda entry: entry.name.lower())
    except OSError as error:
        _record_error(errors, path, error)
        return None


def _favorites_path(root_path):
    return os.path.join(root_path, FAVORITES_FILENAME)


def _favorite_key(relative_path):
    return relative_path.replace(os.sep, "/")


def _path_is_within(root_path, candidate_path):
    try:
        return os.path.commonpath([root_path, candidate_path]) == root_path
    except ValueError:
        return False


def normalize_ignored_directories(root_paths, ignored_directories=None):
    if ignored_directories is None:
        return []
    if not isinstance(ignored_directories, list):
        raise ValueError("跳过扫描目录必须是列表")

    if isinstance(root_paths, str):
        root_paths = [root_paths]
    normalized_roots = [os.path.realpath(root_path) for root_path in root_paths]
    normalized_directories = []
    seen = set()

    for directory in ignored_directories:
        if not isinstance(directory, str):
            raise ValueError("跳过扫描目录格式无效")

        directory = directory.strip()
        if not directory:
            continue

        expanded_directory = os.path.expanduser(directory)
        if not os.path.isabs(expanded_directory):
            raise ValueError(f"跳过扫描目录必须使用绝对路径：{directory}")
        resolved_directory = os.path.realpath(expanded_directory)

        containing_roots = [
            root_path
            for root_path in normalized_roots
            if _path_is_within(root_path, resolved_directory)
        ]
        if not containing_roots:
            raise ValueError(f"跳过扫描目录超出所有游戏根目录：{directory}")
        if resolved_directory in containing_roots:
            raise ValueError("不能跳过录屏根目录")
        if os.path.exists(resolved_directory) and not os.path.isdir(resolved_directory):
            raise ValueError(f"跳过扫描路径不是目录：{directory}")

        supported_depth = any(
            len(os.path.relpath(resolved_directory, root_path).split(os.sep)) <= 2
            for root_path in containing_roots
        )
        if not supported_depth:
            raise ValueError(f"跳过扫描目录只支持游戏或录屏目录：{directory}")

        if resolved_directory not in seen:
            seen.add(resolved_directory)
            normalized_directories.append(resolved_directory)

    return normalized_directories


def _is_ignored_path(path, ignored_directories):
    resolved_path = os.path.realpath(path)
    return any(
        resolved_path == ignored_directory
        or _path_is_within(ignored_directory, resolved_path)
        for ignored_directory in ignored_directories
    )


def _ignored_directories_for_root(root_path, ignored_directories):
    return [
        directory
        for directory in ignored_directories
        if _path_is_within(root_path, directory) and directory != root_path
    ]


def _read_favorites_unlocked(root_path):
    metadata_path = _favorites_path(root_path)
    if os.path.islink(metadata_path):
        raise ValueError("收藏数据文件不能是符号链接")

    try:
        with open(metadata_path, encoding="utf-8") as metadata_file:
            payload = json.load(metadata_file)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise ValueError("收藏数据格式无效") from error

    if not isinstance(payload, dict) or payload.get("version") != FAVORITES_VERSION:
        raise ValueError("收藏数据版本无效")

    favorites = payload.get("favorites")
    if not isinstance(favorites, dict):
        raise ValueError("收藏数据格式无效")

    for relative_path, metadata in favorites.items():
        if not isinstance(relative_path, str) or not isinstance(metadata, dict):
            raise ValueError("收藏数据格式无效")
        favorited_at = metadata.get("favorited_at")
        note = metadata.get("note", "")
        if favorited_at is not None and (
            not isinstance(favorited_at, str) or not favorited_at
        ):
            raise ValueError("收藏数据格式无效")
        if not isinstance(note, str) or len(note) > MAX_NOTE_LENGTH:
            raise ValueError("收藏数据格式无效")
        if favorited_at is None and not note:
            raise ValueError("收藏数据格式无效")

    return favorites


def _read_favorites(root_path):
    with _favorites_lock:
        return _read_favorites_unlocked(root_path)


def _write_favorites_unlocked(root_path, favorites):
    metadata_path = _favorites_path(root_path)
    temporary_path = None
    file_descriptor = None
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            dir=root_path,
            prefix=f"{FAVORITES_FILENAME}.",
            suffix=".tmp",
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as metadata_file:
            file_descriptor = None
            json.dump(
                {"version": FAVORITES_VERSION, "favorites": favorites},
                metadata_file,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            metadata_file.write("\n")
            metadata_file.flush()
            os.fsync(metadata_file.fileno())
        os.replace(temporary_path, metadata_path)
        temporary_path = None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_path is not None:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass


def scan_recordings(root_paths, ignored_directories=None):
    """Scan each root/game_id/recording_directory without following symlinks."""
    if isinstance(root_paths, str):
        root_paths = [root_paths]
    root_paths, validation_error = validate_roots(root_paths)
    if validation_error:
        raise ValueError(validation_error)
    ignored_directories = normalize_ignored_directories(root_paths, ignored_directories)

    errors = []
    games_by_id = {}
    empty_directories = []
    directory_count = 0

    for root_path in root_paths:
        try:
            favorites = _read_favorites(root_path)
        except (OSError, ValueError) as error:
            _record_error(errors, _favorites_path(root_path), error)
            favorites = {}

        for game_entry in _directory_entries(root_path, errors) or ():
            if _is_ignored_path(game_entry.path, ignored_directories):
                continue
            try:
                if not game_entry.is_dir(follow_symlinks=False):
                    continue
            except OSError as error:
                _record_error(errors, game_entry.path, error)
                continue

            game = games_by_id.setdefault(
                game_entry.name,
                {
                    "game_id": game_entry.name,
                    "directory_count": 0,
                    "recordings": [],
                },
            )

            for random_entry in _directory_entries(game_entry.path, errors) or ():
                relative_directory_path = os.path.join(
                    game_entry.name,
                    random_entry.name,
                )
                if _is_ignored_path(random_entry.path, ignored_directories):
                    continue
                try:
                    if not random_entry.is_dir(follow_symlinks=False):
                        continue
                except OSError as error:
                    _record_error(errors, random_entry.path, error)
                    continue

                directory_count += 1
                game["directory_count"] += 1
                entries = _directory_entries(random_entry.path, errors)

                if entries is None:
                    continue

                if not entries:
                    empty_directories.append(
                        {
                            "root": root_path,
                            "game_id": game_entry.name,
                            "directory_id": random_entry.name,
                            "path": relative_directory_path,
                        }
                    )
                    continue

                covers = {}
                videos = []
                for media_entry in entries:
                    try:
                        if not media_entry.is_file(follow_symlinks=False):
                            continue
                    except OSError as error:
                        _record_error(errors, media_entry.path, error)
                        continue

                    stem, extension = os.path.splitext(media_entry.name)
                    extension = extension.lower()
                    if extension in COVER_EXTENSIONS:
                        covers.setdefault(stem.lower(), {})[extension] = media_entry.name
                    elif extension in VIDEO_EXTENSIONS:
                        videos.append(media_entry)

                for video_entry in videos:
                    try:
                        stat = video_entry.stat(follow_symlinks=False)
                    except OSError as error:
                        _record_error(errors, video_entry.path, error)
                        continue

                    stem = os.path.splitext(video_entry.name)[0]
                    matching_covers = covers.get(stem.lower(), {})
                    cover_name = next(
                        (
                            matching_covers[extension]
                            for extension in COVER_EXTENSIONS
                            if extension in matching_covers
                        ),
                        None,
                    )
                    relative_path = os.path.join(
                        relative_directory_path,
                        video_entry.name,
                    )
                    favorite_metadata = favorites.get(_favorite_key(relative_path)) or {}
                    game["recordings"].append(
                        {
                            "root": root_path,
                            "game_id": game_entry.name,
                            "directory_id": random_entry.name,
                            "directory_path": relative_directory_path,
                            "name": video_entry.name,
                            "path": relative_path,
                            "cover_path": (
                                os.path.join(relative_directory_path, cover_name)
                                if cover_name
                                else None
                            ),
                            "size": stat.st_size,
                            "created_at": _recording_created_at(stat),
                            "mtime": stat.st_mtime,
                            # Keep nanoseconds as text so browsers do not round the
                            # value beyond JavaScript's safe integer range.
                            "mtime_ns": str(stat.st_mtime_ns),
                            "favorite": bool(favorite_metadata.get("favorited_at")),
                            "favorited_at": favorite_metadata.get("favorited_at"),
                            "note": favorite_metadata.get("note", ""),
                        }
                    )

    games = sorted(games_by_id.values(), key=lambda game: game["game_id"].lower())
    for game in games:
        game["recordings"].sort(
            key=lambda item: (
                -item["created_at"],
                item["name"].lower(),
                item["root"],
            )
        )

    return {
        "root": root_paths[0],
        "roots": root_paths,
        "summary": {
            "game_count": len(games),
            "directory_count": directory_count,
            "recording_count": sum(len(game["recordings"]) for game in games),
            "empty_directory_count": len(empty_directories),
        },
        "games": games,
        "empty_directories": empty_directories,
        "ignored_directories": ignored_directories,
        "errors": errors,
    }


def _remember_roots(root_paths, ignored_directories=None):
    scan_id = secrets.token_urlsafe(18)
    with _scan_roots_lock:
        _scan_roots[scan_id] = {
            "roots": list(root_paths),
            "ignored_directories": list(ignored_directories or ()),
        }
        _scan_roots.move_to_end(scan_id)
        while len(_scan_roots) > MAX_SCAN_SESSIONS:
            _scan_roots.popitem(last=False)
    return scan_id


def _get_scan_context(scan_id):
    if not isinstance(scan_id, str):
        return None
    with _scan_roots_lock:
        context = _scan_roots.get(scan_id)
        if context:
            _scan_roots.move_to_end(scan_id)
        return context


def _get_scan_root(scan_id, requested_root=None):
    context = _get_scan_context(scan_id)
    if not context:
        return None

    roots = context["roots"]
    if requested_root is None:
        return roots[0] if len(roots) == 1 else None
    if not isinstance(requested_root, str):
        return None

    normalized_root = os.path.realpath(os.path.expanduser(requested_root.strip()))
    return normalized_root if normalized_root in roots else None


def _resolve_relative_path(root_path, relative_path, expected_parts, extensions=None):
    if not isinstance(relative_path, str):
        raise ValueError("路径参数无效")

    parts = relative_path.replace("\\", "/").split("/")
    if len(parts) not in expected_parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("路径参数无效")

    root_path = os.path.realpath(root_path)
    normalized_relative_path = os.path.join(*parts)
    unresolved_path = os.path.join(root_path, normalized_relative_path)
    resolved_path = os.path.realpath(unresolved_path)
    if os.path.commonpath([root_path, resolved_path]) != root_path:
        raise ValueError("路径超出录屏根目录")
    if os.path.islink(unresolved_path):
        raise ValueError("不支持符号链接")
    if extensions and os.path.splitext(resolved_path)[1].lower() not in extensions:
        raise ValueError("文件类型不受支持")

    return resolved_path, normalized_relative_path


def set_recording_favorite(root_path, relative_path, favorite):
    if not isinstance(favorite, bool):
        raise ValueError("收藏状态无效")

    video_path, normalized_relative_path = _resolve_relative_path(
        root_path,
        relative_path,
        expected_parts={3},
        extensions=VIDEO_EXTENSIONS,
    )
    if not os.path.isfile(video_path):
        raise FileNotFoundError("录屏文件不存在，请重新扫描")

    favorite_key = _favorite_key(normalized_relative_path)
    with _favorites_lock:
        favorites = _read_favorites_unlocked(root_path)
        metadata = dict(favorites.get(favorite_key) or {})
        changed = False

        if favorite:
            if not metadata.get("favorited_at"):
                metadata["favorited_at"] = (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                changed = True
        elif metadata.pop("favorited_at", None) is not None:
            changed = True

        if metadata:
            favorites[favorite_key] = metadata
        else:
            favorites.pop(favorite_key, None)
        if changed:
            _write_favorites_unlocked(root_path, favorites)

    return {
        "path": normalized_relative_path,
        "favorite": favorite,
        "favorited_at": metadata.get("favorited_at"),
    }


def set_recording_note(root_path, relative_path, note):
    if not isinstance(note, str):
        raise ValueError("评论格式无效")
    if len(note) > MAX_NOTE_LENGTH:
        raise ValueError(f"评论不能超过 {MAX_NOTE_LENGTH} 个字符")
    note = note.strip()

    video_path, normalized_relative_path = _resolve_relative_path(
        root_path,
        relative_path,
        expected_parts={3},
        extensions=VIDEO_EXTENSIONS,
    )
    if not os.path.isfile(video_path):
        raise FileNotFoundError("录屏文件不存在，请重新扫描")

    favorite_key = _favorite_key(normalized_relative_path)
    with _favorites_lock:
        favorites = _read_favorites_unlocked(root_path)
        metadata = dict(favorites.get(favorite_key) or {})
        previous_note = metadata.get("note", "")

        if note:
            metadata["note"] = note
        else:
            metadata.pop("note", None)

        if metadata:
            favorites[favorite_key] = metadata
        else:
            favorites.pop(favorite_key, None)
        if note != previous_note:
            _write_favorites_unlocked(root_path, favorites)

    return {"path": normalized_relative_path, "note": note}


def _remove_recording_metadata(root_path, normalized_relative_path):
    favorite_key = _favorite_key(normalized_relative_path)
    with _favorites_lock:
        favorites = _read_favorites_unlocked(root_path)
        if favorites.pop(favorite_key, None) is not None:
            _write_favorites_unlocked(root_path, favorites)


def delete_recording(root_path, relative_path, expected_size, expected_mtime_ns):
    video_path, normalized_relative_path = _resolve_relative_path(
        root_path,
        relative_path,
        expected_parts={3},
        extensions=VIDEO_EXTENSIONS,
    )
    if not os.path.isfile(video_path):
        raise FileNotFoundError("录屏文件不存在，请重新扫描")

    try:
        expected_size = int(expected_size)
        expected_mtime_ns = int(expected_mtime_ns)
    except (TypeError, ValueError) as error:
        raise ValueError("录屏校验信息无效") from error

    stat = os.stat(video_path, follow_symlinks=False)
    if stat.st_size != expected_size or stat.st_mtime_ns != expected_mtime_ns:
        raise RuntimeError("录屏文件在扫描后发生变化，请重新扫描")

    os.remove(video_path)
    deleted = [normalized_relative_path]
    video_stem = os.path.splitext(os.path.basename(video_path))[0].lower()

    for entry in os.scandir(os.path.dirname(video_path)):
        stem, extension = os.path.splitext(entry.name)
        try:
            is_regular_file = entry.is_file(follow_symlinks=False)
        except OSError:
            is_regular_file = False
        if is_regular_file and stem.lower() == video_stem and extension.lower() in COVER_EXTENSIONS:
            os.remove(entry.path)
            deleted.append(os.path.join(os.path.dirname(normalized_relative_path), entry.name))

    try:
        _remove_recording_metadata(root_path, normalized_relative_path)
    except (OSError, ValueError) as error:
        logger.warning(
            "Unable to remove recording metadata for %s: %s",
            normalized_relative_path,
            error,
        )

    return deleted


def find_empty_recording_directories(
    root_path,
    ignored_directories=None,
    game_id=None,
):
    root_path, validation_error = validate_root(root_path)
    if validation_error:
        raise ValueError(validation_error)
    ignored_directories = normalize_ignored_directories(root_path, ignored_directories)

    errors = []
    empty_directories = []
    for game_entry in _directory_entries(root_path, errors) or ():
        if game_id is not None and game_entry.name != game_id:
            continue
        if _is_ignored_path(game_entry.path, ignored_directories):
            continue
        try:
            if not game_entry.is_dir(follow_symlinks=False):
                continue
        except OSError as error:
            _record_error(errors, game_entry.path, error)
            continue

        for random_entry in _directory_entries(game_entry.path, errors) or ():
            directory_path = os.path.join(game_entry.name, random_entry.name)
            if _is_ignored_path(random_entry.path, ignored_directories):
                continue
            try:
                if not random_entry.is_dir(follow_symlinks=False):
                    continue
                entries = _directory_entries(random_entry.path, errors)
                if entries == []:
                    empty_directories.append(os.path.join(game_entry.name, random_entry.name))
            except OSError as error:
                _record_error(errors, random_entry.path, error)

    return empty_directories, errors


def delete_empty_recording_directories(
    root_path,
    relative_path=None,
    ignored_directories=None,
    game_id=None,
):
    ignored_directories = normalize_ignored_directories(root_path, ignored_directories)
    if relative_path is None:
        candidates, errors = find_empty_recording_directories(
            root_path,
            ignored_directories,
            game_id,
        )
    else:
        if isinstance(relative_path, str):
            candidate_path = os.path.join(root_path, relative_path)
            if _is_ignored_path(candidate_path, ignored_directories):
                return [], [f"{relative_path}：该目录已设置为跳过扫描"]
        candidates, errors = [relative_path], []

    deleted = []
    for candidate in candidates:
        try:
            directory_path, normalized_relative_path = _resolve_relative_path(
                root_path,
                candidate,
                expected_parts={2},
            )
            os.rmdir(directory_path)
            deleted.append(normalized_relative_path)
        except (OSError, ValueError) as error:
            error_path = candidate if isinstance(candidate, str) else root_path
            _record_error(errors, error_path, error)

    return deleted, errors


def _json_error(message, status_code):
    return jsonify({"success": False, "message": message}), status_code


@game_recording_review_bp.route("/")
def index():
    return render_template("game_recording_review/index.html")


@game_recording_review_bp.route("/api/settings", methods=["GET"])
def api_get_settings():
    try:
        settings = read_settings()
    except ValueError as error:
        return _json_error(str(error), 500)
    except OSError as error:
        return _json_error(error.strerror or str(error), 500)
    return jsonify({"success": True, **settings})


@game_recording_review_bp.route("/api/settings", methods=["PUT"])
def api_save_settings():
    data = request.get_json(silent=True) or {}
    try:
        settings = write_settings(
            data.get("roots"),
            data.get("ignored_directories"),
        )
    except ValueError as error:
        return _json_error(str(error), 400)
    except OSError as error:
        return _json_error(error.strerror or str(error), 500)
    return jsonify({"success": True, **settings})


@game_recording_review_bp.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(silent=True) or {}
    root_paths = data.get("paths")
    if root_paths is None:
        root_paths = [data.get("path")] if data.get("path") is not None else []

    try:
        result = scan_recordings(root_paths, data.get("ignored_directories"))
    except ValueError as error:
        return _json_error(str(error), 400)
    result["scan_id"] = _remember_roots(
        result["roots"],
        result["ignored_directories"],
    )
    result["success"] = not result["errors"]
    return jsonify(result)


@game_recording_review_bp.route("/media/<scan_id>/<path:relative_path>")
def media(scan_id, relative_path):
    scan_context = _get_scan_context(scan_id)
    if not scan_context:
        return _json_error("扫描已失效，请重新扫描", 404)
    root_path = _get_scan_root(scan_id, request.args.get("root"))
    if not root_path:
        return _json_error("媒体所属根目录无效，请重新扫描", 400)

    try:
        media_path, normalized_relative_path = _resolve_relative_path(
            root_path,
            relative_path,
            expected_parts={3},
            extensions=MEDIA_EXTENSIONS,
        )
    except ValueError as error:
        return _json_error(str(error), 400)

    if not os.path.isfile(media_path):
        return _json_error("媒体文件不存在", 404)
    return send_from_directory(root_path, normalized_relative_path, conditional=True)


@game_recording_review_bp.route("/api/recording", methods=["DELETE"])
def api_delete_recording():
    data = request.get_json(silent=True) or {}
    scan_context = _get_scan_context(data.get("scan_id"))
    if not scan_context:
        return _json_error("扫描已失效，请重新扫描", 404)
    root_path = _get_scan_root(data.get("scan_id"), data.get("root"))
    if not root_path:
        return _json_error("录屏所属根目录无效，请重新扫描", 400)

    try:
        deleted = delete_recording(
            root_path,
            data.get("path"),
            data.get("size"),
            data.get("mtime_ns"),
        )
    except ValueError as error:
        return _json_error(str(error), 400)
    except FileNotFoundError as error:
        return _json_error(str(error), 404)
    except RuntimeError as error:
        return _json_error(str(error), 409)
    except OSError as error:
        return _json_error(error.strerror or str(error), 409)

    return jsonify({"success": True, "deleted": deleted})


@game_recording_review_bp.route("/api/recordings", methods=["DELETE"])
def api_delete_recordings():
    data = request.get_json(silent=True) or {}
    if not _get_scan_context(data.get("scan_id")):
        return _json_error("扫描已失效，请重新扫描", 404)

    recordings = data.get("recordings")
    if not isinstance(recordings, list) or not recordings:
        return _json_error("请至少选择一个录屏", 400)
    if len(recordings) > MAX_BATCH_RECORDINGS:
        return _json_error(f"单次最多删除 {MAX_BATCH_RECORDINGS} 个录屏", 400)

    deleted_recordings = []
    errors = []
    seen = set()
    for recording in recordings:
        if not isinstance(recording, dict):
            errors.append({"root": None, "path": None, "message": "录屏参数无效"})
            continue

        root_path = _get_scan_root(data.get("scan_id"), recording.get("root"))
        relative_path = recording.get("path")
        if not root_path:
            errors.append(
                {
                    "root": recording.get("root"),
                    "path": relative_path,
                    "message": "录屏所属根目录无效，请重新扫描",
                }
            )
            continue
        if not isinstance(relative_path, str):
            errors.append(
                {
                    "root": root_path,
                    "path": None,
                    "message": "录屏路径参数无效",
                }
            )
            continue

        key = (root_path, relative_path)
        if key in seen:
            continue
        seen.add(key)

        try:
            deleted_files = delete_recording(
                root_path,
                relative_path,
                recording.get("size"),
                recording.get("mtime_ns"),
            )
        except (ValueError, FileNotFoundError, RuntimeError, OSError) as error:
            errors.append(
                {
                    "root": root_path,
                    "path": relative_path,
                    "message": getattr(error, "strerror", None) or str(error),
                }
            )
            continue

        deleted_recordings.append(
            {
                "root": root_path,
                "path": relative_path,
                "files": deleted_files,
            }
        )

    return jsonify(
        {
            "success": not errors,
            "deleted": deleted_recordings,
            "deleted_count": len(deleted_recordings),
            "errors": errors,
        }
    )


@game_recording_review_bp.route("/api/recording/favorite", methods=["PATCH"])
def api_set_recording_favorite():
    data = request.get_json(silent=True) or {}
    scan_context = _get_scan_context(data.get("scan_id"))
    if not scan_context:
        return _json_error("扫描已失效，请重新扫描", 404)
    root_path = _get_scan_root(data.get("scan_id"), data.get("root"))
    if not root_path:
        return _json_error("录屏所属根目录无效，请重新扫描", 400)

    try:
        result = set_recording_favorite(
            root_path,
            data.get("path"),
            data.get("favorite"),
        )
    except ValueError as error:
        return _json_error(str(error), 400)
    except FileNotFoundError as error:
        return _json_error(str(error), 404)
    except OSError as error:
        return _json_error(error.strerror or str(error), 409)

    return jsonify({"success": True, **result})


@game_recording_review_bp.route("/api/recording/note", methods=["PATCH"])
def api_set_recording_note():
    data = request.get_json(silent=True) or {}
    if not _get_scan_context(data.get("scan_id")):
        return _json_error("扫描已失效，请重新扫描", 404)
    root_path = _get_scan_root(data.get("scan_id"), data.get("root"))
    if not root_path:
        return _json_error("录屏所属根目录无效，请重新扫描", 400)

    try:
        result = set_recording_note(
            root_path,
            data.get("path"),
            data.get("note"),
        )
    except ValueError as error:
        return _json_error(str(error), 400)
    except FileNotFoundError as error:
        return _json_error(str(error), 404)
    except OSError as error:
        return _json_error(error.strerror or str(error), 409)

    return jsonify({"success": True, **result})


@game_recording_review_bp.route("/api/empty-directories", methods=["DELETE"])
def api_delete_empty_directories():
    data = request.get_json(silent=True) or {}
    scan_context = _get_scan_context(data.get("scan_id"))
    if not scan_context:
        return _json_error("扫描已失效，请重新扫描", 404)

    requested_directories = data.get("directories")
    deletion_jobs = []
    if requested_directories is not None:
        if not isinstance(requested_directories, list):
            return _json_error("空目录列表无效", 400)
        seen = set()
        for directory in requested_directories:
            if not isinstance(directory, dict):
                return _json_error("空目录列表无效", 400)
            root_path = _get_scan_root(
                data.get("scan_id"),
                directory.get("root"),
            )
            relative_path = directory.get("path")
            if not root_path or not isinstance(relative_path, str):
                return _json_error("空目录所属根目录或路径无效，请重新扫描", 400)
            key = (root_path, relative_path)
            if key not in seen:
                seen.add(key)
                deletion_jobs.append((root_path, relative_path, None))
    else:
        relative_path = data.get("path")
        game_id = data.get("game_id")
        if game_id is not None and not isinstance(game_id, str):
            return _json_error("游戏 ID 无效", 400)

        if relative_path is None:
            deletion_jobs = [
                (root_path, None, game_id)
                for root_path in scan_context["roots"]
            ]
        else:
            root_path = _get_scan_root(data.get("scan_id"), data.get("root"))
            if not root_path:
                return _json_error("空目录所属根目录无效，请重新扫描", 400)
            deletion_jobs = [(root_path, relative_path, game_id)]

    deleted = []
    errors = []
    for root_path, relative_path, game_id in deletion_jobs:
        root_deleted, root_errors = delete_empty_recording_directories(
            root_path,
            relative_path,
            _ignored_directories_for_root(
                root_path,
                scan_context["ignored_directories"],
            ),
            game_id,
        )
        deleted.extend(root_deleted)
        errors.extend(root_errors)
    return jsonify(
        {
            "success": not errors,
            "deleted": deleted,
            "errors": errors,
            "message": f"已删除 {len(deleted)} 个空目录",
        }
    )
