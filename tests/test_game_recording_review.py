import json
import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from tools.game_recording_review.routes import (
    FAVORITES_FILENAME,
    MAX_NOTE_LENGTH,
    SETTINGS_CONFIG_DIR_ENV,
    SETTINGS_FILENAME,
    delete_empty_recording_directories,
    delete_recording,
    game_recording_review_bp,
    normalize_ignored_directories,
    read_settings,
    scan_recordings,
    set_recording_favorite,
    set_recording_note,
    write_settings,
)


class GameRecordingReviewTestCase(unittest.TestCase):
    def create_recording_tree(self, root):
        random_dir = os.path.join(root, "18284674457949499456", "648524933912092428")
        os.makedirs(random_dir)
        video_path = os.path.join(random_dir, "53d3feac60d0ee1c194e0fe220339e3e.mp4")
        cover_path = os.path.join(random_dir, "53d3feac60d0ee1c194e0fe220339e3e.jpeg")
        second_video = os.path.join(random_dir, "second.mp4")
        for path, content in (
            (video_path, b"video-one"),
            (cover_path, b"cover"),
            (second_video, b"video-two"),
        ):
            with open(path, "wb") as file:
                file.write(content)

        empty_dir = os.path.join(root, "18284674457949499456", "empty-directory")
        os.mkdir(empty_dir)
        orphan_dir = os.path.join(root, "18284674457949499456", "cover-only")
        os.mkdir(orphan_dir)
        with open(os.path.join(orphan_dir, "orphan.jpeg"), "wb") as file:
            file.write(b"orphan")
        return video_path, cover_path, second_video, empty_dir, orphan_dir

    def test_settings_persist_in_config_directory_json(self):
        with tempfile.TemporaryDirectory() as config_directory, tempfile.TemporaryDirectory() as root:
            ignored_directory = os.path.join(root, "ignored-game")
            os.mkdir(ignored_directory)

            with patch.dict(
                os.environ,
                {SETTINGS_CONFIG_DIR_ENV: config_directory},
            ):
                saved = write_settings([root], [ignored_directory])
                loaded = read_settings()

            settings_path = os.path.join(config_directory, SETTINGS_FILENAME)
            self.assertTrue(os.path.isfile(settings_path))
            self.assertEqual(saved, loaded)
            self.assertEqual(saved["roots"], [os.path.realpath(root)])
            self.assertEqual(
                saved["ignored_directories"],
                [os.path.realpath(ignored_directory)],
            )
            with open(settings_path, encoding="utf-8") as settings_file:
                payload = json.load(settings_file)
            self.assertEqual(payload["version"], 1)

    def test_settings_api_reads_and_writes_config_file(self):
        with tempfile.TemporaryDirectory() as config_directory, tempfile.TemporaryDirectory() as root:
            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()

            with patch.dict(
                os.environ,
                {SETTINGS_CONFIG_DIR_ENV: config_directory},
            ):
                empty_response = client.get(
                    "/tools/game-recording-review/api/settings"
                )
                save_response = client.put(
                    "/tools/game-recording-review/api/settings",
                    json={"roots": [root], "ignored_directories": []},
                )
                load_response = client.get(
                    "/tools/game-recording-review/api/settings"
                )

            self.assertEqual(empty_response.status_code, 200)
            self.assertEqual(empty_response.get_json()["roots"], [])
            self.assertEqual(save_response.status_code, 200)
            self.assertEqual(load_response.status_code, 200)
            self.assertEqual(
                load_response.get_json()["roots"],
                [os.path.realpath(root)],
            )

    def test_settings_api_rejects_malformed_config_json(self):
        with tempfile.TemporaryDirectory() as config_directory:
            settings_path = os.path.join(config_directory, SETTINGS_FILENAME)
            with open(settings_path, "w", encoding="utf-8") as settings_file:
                settings_file.write("not json")

            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()
            with patch.dict(
                os.environ,
                {SETTINGS_CONFIG_DIR_ENV: config_directory},
            ):
                response = client.get("/tools/game-recording-review/api/settings")

            self.assertEqual(response.status_code, 500)
            self.assertIn("JSON", response.get_json()["message"])

    def test_scan_groups_multiple_videos_and_finds_only_truly_empty_directories(self):
        with tempfile.TemporaryDirectory() as root:
            self.create_recording_tree(root)

            result = scan_recordings(root)

            self.assertEqual(result["summary"]["game_count"], 1)
            self.assertEqual(result["summary"]["directory_count"], 3)
            self.assertEqual(result["summary"]["recording_count"], 2)
            self.assertEqual(result["summary"]["empty_directory_count"], 1)
            recordings = result["games"][0]["recordings"]
            covered = next(item for item in recordings if item["name"].startswith("53d3"))
            covered_stat = os.stat(os.path.join(root, covered["path"]))
            expected_created_at = getattr(
                covered_stat,
                "st_birthtime",
                covered_stat.st_ctime if os.name == "nt" else covered_stat.st_mtime,
            )
            self.assertTrue(covered["cover_path"].endswith(".jpeg"))
            self.assertEqual(covered["created_at"], expected_created_at)
            self.assertFalse(covered["favorite"])
            self.assertIsNone(covered["favorited_at"])
            self.assertEqual(covered["note"], "")
            self.assertEqual(result["empty_directories"][0]["directory_id"], "empty-directory")
            self.assertEqual(result["errors"], [])

    def test_scan_skips_configured_game_and_recording_directories(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, _, empty_dir, _ = self.create_recording_tree(root)
            skipped_game = os.path.join(root, "skipped-game")
            skipped_recording_dir = os.path.join(skipped_game, "recording-directory")
            os.makedirs(skipped_recording_dir)
            with open(os.path.join(skipped_recording_dir, "skipped.mp4"), "wb") as file:
                file.write(b"skipped-video")

            result = scan_recordings(
                root,
                [empty_dir, skipped_game],
            )

            self.assertEqual(result["summary"]["game_count"], 1)
            self.assertEqual(result["summary"]["directory_count"], 2)
            self.assertEqual(result["summary"]["recording_count"], 2)
            self.assertEqual(result["summary"]["empty_directory_count"], 0)
            self.assertEqual(
                result["ignored_directories"],
                [
                    os.path.realpath(empty_dir),
                    os.path.realpath(skipped_game),
                ],
            )
            self.assertNotIn(
                "skipped-game",
                [game["game_id"] for game in result["games"]],
            )

    def test_scan_merges_same_game_id_across_multiple_roots(self):
        with tempfile.TemporaryDirectory() as first_root, tempfile.TemporaryDirectory() as second_root:
            self.create_recording_tree(first_root)
            self.create_recording_tree(second_root)
            other_directory = os.path.join(second_root, "another-game", "recording")
            os.makedirs(other_directory)
            with open(os.path.join(other_directory, "other.mp4"), "wb") as file:
                file.write(b"other-video")

            result = scan_recordings([first_root, second_root])

            self.assertEqual(
                result["roots"],
                [os.path.realpath(first_root), os.path.realpath(second_root)],
            )
            self.assertEqual(result["summary"]["game_count"], 2)
            self.assertEqual(result["summary"]["recording_count"], 5)
            merged_game = next(
                game
                for game in result["games"]
                if game["game_id"] == "18284674457949499456"
            )
            self.assertEqual(len(merged_game["recordings"]), 4)
            self.assertEqual(
                {recording["root"] for recording in merged_game["recordings"]},
                {os.path.realpath(first_root), os.path.realpath(second_root)},
            )

    def test_absolute_ignored_directories_apply_across_multiple_roots(self):
        with tempfile.TemporaryDirectory() as first_root, tempfile.TemporaryDirectory() as second_root:
            _, _, _, first_empty, _ = self.create_recording_tree(first_root)
            self.create_recording_tree(second_root)
            second_game = os.path.join(second_root, "18284674457949499456")

            result = scan_recordings(
                [first_root, second_root],
                [first_empty, second_game],
            )

            self.assertEqual(
                result["ignored_directories"],
                [os.path.realpath(first_empty), os.path.realpath(second_game)],
            )
            self.assertEqual(result["summary"]["recording_count"], 2)
            self.assertEqual(result["summary"]["empty_directory_count"], 0)
            recordings = result["games"][0]["recordings"]
            self.assertTrue(
                all(
                    recording["root"] == os.path.realpath(first_root)
                    for recording in recordings
                )
            )

    def test_ignored_directory_validation_rejects_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            video_path, _, _, _, _ = self.create_recording_tree(root)

            with self.assertRaisesRegex(ValueError, "必须是列表"):
                normalize_ignored_directories(root, "game")
            with self.assertRaisesRegex(ValueError, "必须使用绝对路径"):
                normalize_ignored_directories(root, ["game"])
            with self.assertRaisesRegex(ValueError, "超出所有游戏根目录"):
                normalize_ignored_directories(root, [outside])
            with self.assertRaisesRegex(ValueError, "不能跳过录屏根目录"):
                normalize_ignored_directories(root, [root])
            with self.assertRaisesRegex(ValueError, "不是目录"):
                normalize_ignored_directories(root, [video_path])
            with self.assertRaisesRegex(ValueError, "只支持游戏或录屏目录"):
                normalize_ignored_directories(
                    root,
                    [os.path.join(root, "game", "directory", "nested")],
                )

    def test_favorite_persists_in_root_metadata_and_scan_result(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, _, _, _, _ = self.create_recording_tree(root)
            relative_path = os.path.relpath(video_path, root)

            favorite = set_recording_favorite(root, relative_path, True)

            self.assertTrue(favorite["favorite"])
            self.assertTrue(favorite["favorited_at"].endswith("Z"))
            metadata_path = os.path.join(root, FAVORITES_FILENAME)
            with open(metadata_path, encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
            favorite_key = relative_path.replace(os.sep, "/")
            self.assertIn(favorite_key, metadata["favorites"])

            scanned = scan_recordings(root)
            recording = next(
                item
                for game in scanned["games"]
                for item in game["recordings"]
                if item["path"] == relative_path
            )
            self.assertTrue(recording["favorite"])
            self.assertEqual(recording["favorited_at"], favorite["favorited_at"])
            self.assertEqual(scanned["summary"]["game_count"], 1)

            unfavorite = set_recording_favorite(root, relative_path, False)
            self.assertFalse(unfavorite["favorite"])
            self.assertIsNone(unfavorite["favorited_at"])
            recording = next(
                item
                for game in scan_recordings(root)["games"]
                for item in game["recordings"]
                if item["path"] == relative_path
            )
            self.assertFalse(recording["favorite"])

    def test_scan_reports_malformed_favorite_metadata_without_hiding_recordings(self):
        with tempfile.TemporaryDirectory() as root:
            self.create_recording_tree(root)
            with open(os.path.join(root, FAVORITES_FILENAME), "w", encoding="utf-8") as metadata_file:
                metadata_file.write("not json")

            result = scan_recordings(root)

            self.assertEqual(result["summary"]["recording_count"], 2)
            self.assertEqual(len(result["errors"]), 1)
            self.assertIn(FAVORITES_FILENAME, result["errors"][0])
            self.assertTrue(
                all(
                    not recording["favorite"]
                    for game in result["games"]
                    for recording in game["recordings"]
                )
            )

    def test_note_persists_and_favorite_updates_preserve_it(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, _, _, _, _ = self.create_recording_tree(root)
            relative_path = os.path.relpath(video_path, root)

            saved = set_recording_note(root, relative_path, "精彩团战\n五杀")
            self.assertEqual(saved["note"], "精彩团战\n五杀")

            set_recording_favorite(root, relative_path, True)
            set_recording_favorite(root, relative_path, False)
            recording = next(
                item
                for game in scan_recordings(root)["games"]
                for item in game["recordings"]
                if item["path"] == relative_path
            )
            self.assertFalse(recording["favorite"])
            self.assertEqual(recording["note"], "精彩团战\n五杀")

            set_recording_note(root, relative_path, "")
            with open(os.path.join(root, FAVORITES_FILENAME), encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
            self.assertNotIn(relative_path.replace(os.sep, "/"), metadata["favorites"])

    def test_note_api_validates_and_survives_a_new_scan(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, _, _, _, _ = self.create_recording_tree(root)
            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()
            scan_data = client.post(
                "/tools/game-recording-review/api/scan",
                json={"path": root},
            ).get_json()
            relative_path = os.path.relpath(video_path, root)

            response = client.patch(
                "/tools/game-recording-review/api/recording/note",
                json={
                    "scan_id": scan_data["scan_id"],
                    "path": relative_path,
                    "note": "  高光时刻  ",
                },
            )
            too_long = client.patch(
                "/tools/game-recording-review/api/recording/note",
                json={
                    "scan_id": scan_data["scan_id"],
                    "path": relative_path,
                    "note": "x" * (MAX_NOTE_LENGTH + 1),
                },
            )
            invalid = client.patch(
                "/tools/game-recording-review/api/recording/note",
                json={
                    "scan_id": scan_data["scan_id"],
                    "path": relative_path,
                    "note": ["not", "text"],
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["note"], "高光时刻")
            self.assertEqual(too_long.status_code, 400)
            self.assertEqual(invalid.status_code, 400)
            rescanned = client.post(
                "/tools/game-recording-review/api/scan",
                json={"path": root},
            ).get_json()
            recording = next(
                item
                for game in rescanned["games"]
                for item in game["recordings"]
                if item["path"] == relative_path
            )
            self.assertEqual(recording["note"], "高光时刻")

    def test_delete_recording_removes_its_cover_and_preserves_other_video(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, cover_path, second_video, _, _ = self.create_recording_tree(root)
            stat = os.stat(video_path)
            relative_path = os.path.relpath(video_path, root)

            deleted = delete_recording(root, relative_path, stat.st_size, stat.st_mtime_ns)

            self.assertEqual(len(deleted), 2)
            self.assertFalse(os.path.exists(video_path))
            self.assertFalse(os.path.exists(cover_path))
            self.assertTrue(os.path.isfile(second_video))

    def test_delete_recording_removes_its_favorite_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, _, _, _, _ = self.create_recording_tree(root)
            stat = os.stat(video_path)
            relative_path = os.path.relpath(video_path, root)
            set_recording_favorite(root, relative_path, True)

            delete_recording(root, relative_path, stat.st_size, stat.st_mtime_ns)

            with open(os.path.join(root, FAVORITES_FILENAME), encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
            self.assertNotIn(relative_path.replace(os.sep, "/"), metadata["favorites"])

    def test_delete_recording_rejects_a_file_changed_after_scan(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, _, _, _, _ = self.create_recording_tree(root)
            stat = os.stat(video_path)
            with open(video_path, "ab") as file:
                file.write(b"changed")

            with self.assertRaisesRegex(RuntimeError, "重新扫描"):
                delete_recording(
                    root,
                    os.path.relpath(video_path, root),
                    stat.st_size,
                    stat.st_mtime_ns,
                )

            self.assertTrue(os.path.isfile(video_path))

    def test_api_deletes_recording_with_browser_safe_mtime(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, cover_path, second_video, _, _ = self.create_recording_tree(root)
            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()

            scan_response = client.post(
                "/tools/game-recording-review/api/scan",
                json={"path": root},
            )
            scan_data = scan_response.get_json()
            recording = next(
                item
                for game in scan_data["games"]
                for item in game["recordings"]
                if item["name"].startswith("53d3")
            )
            self.assertIsInstance(recording["mtime_ns"], str)

            delete_response = client.delete(
                "/tools/game-recording-review/api/recording",
                json={
                    "scan_id": scan_data["scan_id"],
                    "path": recording["path"],
                    "size": recording["size"],
                    "mtime_ns": recording["mtime_ns"],
                },
            )

            self.assertEqual(delete_response.status_code, 200)
            self.assertFalse(os.path.exists(video_path))
            self.assertFalse(os.path.exists(cover_path))
            self.assertTrue(os.path.isfile(second_video))

    def test_batch_delete_api_removes_selected_recordings(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, cover_path, second_video, _, _ = self.create_recording_tree(root)
            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()
            scan_data = client.post(
                "/tools/game-recording-review/api/scan",
                json={"path": root},
            ).get_json()
            recordings = [
                recording
                for game in scan_data["games"]
                for recording in game["recordings"]
            ]

            response = client.delete(
                "/tools/game-recording-review/api/recordings",
                json={
                    "scan_id": scan_data["scan_id"],
                    "recordings": [
                        {
                            "root": recording["root"],
                            "path": recording["path"],
                            "size": recording["size"],
                            "mtime_ns": recording["mtime_ns"],
                        }
                        for recording in recordings
                    ],
                },
            )

            data = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(data["success"])
            self.assertEqual(data["deleted_count"], 2)
            self.assertEqual(data["errors"], [])
            self.assertFalse(os.path.exists(video_path))
            self.assertFalse(os.path.exists(cover_path))
            self.assertFalse(os.path.exists(second_video))

    def test_batch_delete_api_reports_partial_failures(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, cover_path, second_video, _, _ = self.create_recording_tree(root)
            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()
            scan_data = client.post(
                "/tools/game-recording-review/api/scan",
                json={"path": root},
            ).get_json()
            recordings = [
                recording
                for game in scan_data["games"]
                for recording in game["recordings"]
            ]
            with open(video_path, "ab") as file:
                file.write(b"changed")

            response = client.delete(
                "/tools/game-recording-review/api/recordings",
                json={
                    "scan_id": scan_data["scan_id"],
                    "recordings": [
                        {
                            "root": recording["root"],
                            "path": recording["path"],
                            "size": recording["size"],
                            "mtime_ns": recording["mtime_ns"],
                        }
                        for recording in recordings
                    ],
                },
            )

            data = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertFalse(data["success"])
            self.assertEqual(data["deleted_count"], 1)
            self.assertEqual(len(data["errors"]), 1)
            self.assertTrue(os.path.isfile(video_path))
            self.assertTrue(os.path.isfile(cover_path))
            self.assertFalse(os.path.exists(second_video))

            malformed_response = client.delete(
                "/tools/game-recording-review/api/recordings",
                json={
                    "scan_id": scan_data["scan_id"],
                    "recordings": [{"root": root, "path": []}],
                },
            )
            self.assertEqual(malformed_response.status_code, 200)
            self.assertEqual(len(malformed_response.get_json()["errors"]), 1)

    def test_favorite_api_updates_and_survives_a_new_scan(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, _, _, _, _ = self.create_recording_tree(root)
            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()

            scan_data = client.post(
                "/tools/game-recording-review/api/scan",
                json={"path": root},
            ).get_json()
            relative_path = os.path.relpath(video_path, root)

            favorite_response = client.patch(
                "/tools/game-recording-review/api/recording/favorite",
                json={
                    "scan_id": scan_data["scan_id"],
                    "path": relative_path,
                    "favorite": True,
                },
            )

            self.assertEqual(favorite_response.status_code, 200)
            self.assertTrue(favorite_response.get_json()["favorite"])
            rescanned = client.post(
                "/tools/game-recording-review/api/scan",
                json={"path": root},
            ).get_json()
            recording = next(
                item
                for game in rescanned["games"]
                for item in game["recordings"]
                if item["path"] == relative_path
            )
            self.assertTrue(recording["favorite"])

    def test_favorite_api_rejects_invalid_state_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            self.create_recording_tree(root)
            app = Flask(__name__)
            app.register_blueprint(game_recording_review_bp, url_prefix="/tools/game-recording-review")
            client = app.test_client()
            scan_id = client.post(
                "/tools/game-recording-review/api/scan",
                json={"path": root},
            ).get_json()["scan_id"]

            invalid_state = client.patch(
                "/tools/game-recording-review/api/recording/favorite",
                json={
                    "scan_id": scan_id,
                    "path": "game/directory/video.mp4",
                    "favorite": "yes",
                },
            )
            traversal = client.patch(
                "/tools/game-recording-review/api/recording/favorite",
                json={
                    "scan_id": scan_id,
                    "path": "../../outside.mp4",
                    "favorite": True,
                },
            )

            self.assertEqual(invalid_state.status_code, 400)
            self.assertEqual(traversal.status_code, 400)

    def test_empty_cleanup_preserves_nonempty_directories_and_game_directory(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, _, empty_dir, orphan_dir = self.create_recording_tree(root)

            deleted, errors = delete_empty_recording_directories(root)

            self.assertEqual(errors, [])
            self.assertEqual(deleted, [os.path.relpath(empty_dir, root)])
            self.assertFalse(os.path.exists(empty_dir))
            self.assertTrue(os.path.isdir(orphan_dir))
            self.assertTrue(os.path.isdir(os.path.dirname(orphan_dir)))

    def test_empty_cleanup_keeps_directories_ignored_by_scan(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, _, empty_dir, _ = self.create_recording_tree(root)
            ignored_directory = empty_dir
            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()

            scan_response = client.post(
                "/tools/game-recording-review/api/scan",
                json={
                    "path": root,
                    "ignored_directories": [ignored_directory],
                },
            )
            scan_data = scan_response.get_json()
            cleanup_response = client.delete(
                "/tools/game-recording-review/api/empty-directories",
                json={"scan_id": scan_data["scan_id"], "path": None},
            )
            direct_cleanup_response = client.delete(
                "/tools/game-recording-review/api/empty-directories",
                json={
                    "scan_id": scan_data["scan_id"],
                    "path": os.path.relpath(ignored_directory, root),
                },
            )

            self.assertEqual(scan_response.status_code, 200)
            self.assertEqual(
                scan_data["ignored_directories"],
                [os.path.realpath(ignored_directory)],
            )
            self.assertEqual(scan_data["summary"]["empty_directory_count"], 0)
            self.assertEqual(cleanup_response.get_json()["deleted"], [])
            self.assertTrue(direct_cleanup_response.get_json()["errors"])
            self.assertTrue(os.path.isdir(empty_dir))

    def test_empty_cleanup_targets_visible_directories_across_roots(self):
        with tempfile.TemporaryDirectory() as first_root, tempfile.TemporaryDirectory() as second_root:
            _, _, _, first_empty, _ = self.create_recording_tree(first_root)
            _, _, _, second_empty, _ = self.create_recording_tree(second_root)
            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()
            scan_data = client.post(
                "/tools/game-recording-review/api/scan",
                json={"paths": [first_root, second_root]},
            ).get_json()
            first_directory = next(
                directory
                for directory in scan_data["empty_directories"]
                if directory["root"] == os.path.realpath(first_root)
            )

            response = client.delete(
                "/tools/game-recording-review/api/empty-directories",
                json={
                    "scan_id": scan_data["scan_id"],
                    "directories": [
                        {
                            "root": first_directory["root"],
                            "path": first_directory["path"],
                        }
                    ],
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.get_json()["deleted"]), 1)
            self.assertFalse(os.path.exists(first_empty))
            self.assertTrue(os.path.isdir(second_empty))

    def test_scan_api_rejects_an_ignored_directory_outside_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            self.create_recording_tree(root)
            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()

            response = client.post(
                "/tools/game-recording-review/api/scan",
                json={"path": root, "ignored_directories": [outside]},
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("超出所有游戏根目录", response.get_json()["message"])

    def test_api_serves_media_and_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            video_path, _, _, _, _ = self.create_recording_tree(root)
            app = Flask(__name__)
            app.register_blueprint(game_recording_review_bp, url_prefix="/tools/game-recording-review")
            client = app.test_client()

            scan_response = client.post(
                "/tools/game-recording-review/api/scan",
                json={"path": root},
            )
            self.assertEqual(scan_response.status_code, 200)
            scan_id = scan_response.get_json()["scan_id"]
            relative_path = os.path.relpath(video_path, root)

            media_response = client.get(
                f"/tools/game-recording-review/media/{scan_id}/{relative_path}"
            )
            self.assertEqual(media_response.status_code, 200)
            self.assertEqual(media_response.data, b"video-one")
            media_response.close()

            traversal_response = client.delete(
                "/tools/game-recording-review/api/recording",
                json={
                    "scan_id": scan_id,
                    "path": "../../outside.mp4",
                    "size": 0,
                    "mtime_ns": 0,
                },
            )
            self.assertEqual(traversal_response.status_code, 400)

    def test_multi_root_api_scopes_media_and_favorites_to_source_root(self):
        with tempfile.TemporaryDirectory() as first_root, tempfile.TemporaryDirectory() as second_root:
            first_video, _, _, _, _ = self.create_recording_tree(first_root)
            second_video, _, _, _, _ = self.create_recording_tree(second_root)
            with open(second_video, "wb") as file:
                file.write(b"video-from-second-root")

            app = Flask(__name__)
            app.register_blueprint(
                game_recording_review_bp,
                url_prefix="/tools/game-recording-review",
            )
            client = app.test_client()
            scan_data = client.post(
                "/tools/game-recording-review/api/scan",
                json={"paths": [first_root, second_root]},
            ).get_json()
            relative_path = os.path.relpath(second_video, second_root)

            ambiguous_media = client.get(
                f"/tools/game-recording-review/media/{scan_data['scan_id']}/{relative_path}"
            )
            scoped_media = client.get(
                f"/tools/game-recording-review/media/{scan_data['scan_id']}/{relative_path}",
                query_string={"root": second_root},
            )
            favorite_response = client.patch(
                "/tools/game-recording-review/api/recording/favorite",
                json={
                    "scan_id": scan_data["scan_id"],
                    "root": second_root,
                    "path": relative_path,
                    "favorite": True,
                },
            )

            self.assertEqual(ambiguous_media.status_code, 400)
            self.assertEqual(scoped_media.status_code, 200)
            self.assertEqual(scoped_media.data, b"video-from-second-root")
            scoped_media.close()
            self.assertEqual(favorite_response.status_code, 200)

            rescanned = scan_recordings([first_root, second_root])
            same_path_recordings = [
                recording
                for game in rescanned["games"]
                for recording in game["recordings"]
                if recording["path"] == relative_path
            ]
            self.assertEqual(len(same_path_recordings), 2)
            self.assertFalse(
                next(
                    recording
                    for recording in same_path_recordings
                    if recording["root"] == os.path.realpath(first_root)
                )["favorite"]
            )
            self.assertTrue(
                next(
                    recording
                    for recording in same_path_recordings
                    if recording["root"] == os.path.realpath(second_root)
                )["favorite"]
            )
            self.assertTrue(os.path.isfile(first_video))


if __name__ == "__main__":
    unittest.main()
