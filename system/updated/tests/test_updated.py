import contextlib
import dataclasses
import http
import json
import os
import pathlib
import time
from unittest import mock

from openpilot.selfdrive.test.helpers import DirectoryHttpServer, http_server_context, processes_context
from openpilot.system.hardware.tici.agnos import get_raw_hash
from openpilot.system.updated.casync.common import create_build_metadata_file, create_casync_from_file, create_casync_release
from openpilot.system.version import BuildMetadata, OpenpilotMetadata
from openpilot.selfdrive.updated.tests.test_base import BaseUpdateTest, run, update_release, get_consistent_flag


def create_remote_response(channel, build_metadata, entries: list[dict], casync_base: str):

  for entry in entries:
    entry["casync"]["caibx"] = os.path.join(casync_base, os.path.basename(entry["casync"]["caibx"]))

  return {
    "build_metadata": dataclasses.asdict(build_metadata),
    "manifest": entries
  }


def create_mock_build_metadata(channel, version, agnos_version, release_notes):
  commit = str(hash((version, agnos_version, release_notes)))  # simulated commit
  return BuildMetadata(channel, OpenpilotMetadata(version, release_notes, commit, "https://github.com/commaai/openpilot.git", "2024", "release", False))


def create_casync_files(dirname, channel, version, agnos_version, release_notes):
  create_build_metadata_file(pathlib.Path(dirname), create_mock_build_metadata(channel, version, agnos_version, release_notes), channel)


def OpenpilotChannelMockAPI(release_manifests: dict[str, list[dict]], mock_releases, casync_base):
  class Handler(http.server.BaseHTTPRequestHandler):
    API_BAD_RESPONSE = False
    API_NO_RESPONSE = False

    def do_GET(self):
      if self.API_NO_RESPONSE:
        return

      if self.API_BAD_RESPONSE:
        self.send_response(500, "")
        return

      if self.path == "/v1/openpilot/channels":
        response = list(release_manifests.keys())
      else:
        channel = self.path.split("/")[-1]
        build_metadata = create_mock_build_metadata(channel, *mock_releases[channel])

        response = create_remote_response(channel, build_metadata, release_manifests[channel], casync_base)

      response = json.dumps(response)

      self.send_response(200)
      self.send_header('Content-Type', 'application/json')
      self.end_headers()
      self.wfile.write(response.encode(encoding='utf_8'))

  return Handler


def create_virtual_agnos_manifest(mock_update_path: pathlib.Path, agnos_version: str) -> list[dict]:
  agnos_bin_file = mock_update_path / "agnos.bin"

  data = agnos_version.encode("utf-8")

  with open(agnos_bin_file, "wb") as f:
    f.write(data)

  caibx_file = create_casync_from_file(agnos_bin_file, mock_update_path / "casync", "agnos.caibx")

  size = len(data)
  raw_hash = get_raw_hash(str(agnos_bin_file), size)

  return [
    {
      "name": "system",
      "type": "partition",
      "casync": {
        "caibx": caibx_file.name
      },
      "size": size,
      "hash_raw": raw_hash,
      "full_check": True
    },
    {
      "name": "boot",
      "type": "partition",
      "casync": {
        "caibx": caibx_file.name
      },
      "size": size,
      "hash_raw": raw_hash,
      "full_check": False
    },
  ]


class TestUpdated(BaseUpdateTest):
  def setUp(self):
    super().setUp()
    self.casync_dir = self.mock_update_path / "casync"
    self.casync_dir.mkdir()
    self.release_manifests = {}
    os.environ["UPDATE_DELAY"] = "1"

    self.system_a = self.mock_update_path / "system_a"
    self.system_b = self.mock_update_path / "system_b"

    self.system_a.write_bytes(b"1.2")
    self.system_b.write_bytes(b"1.2")

    self.boot_a = self.mock_update_path / "boot_a"
    self.boot_b = self.mock_update_path / "boot_b"

    self.boot_a.write_bytes(b"1.2")
    self.boot_b.write_bytes(b"1.2")

  def update_remote_release(self, release):
    update_release(self.remote_dir, release, *self.MOCK_RELEASES[release])
    create_casync_files(self.remote_dir, release, *self.MOCK_RELEASES[release])

    digest, caibx_file = create_casync_release(self.remote_dir, self.casync_dir, release)
    self.release_manifests[release] = [
      {
        "type": "path_tarred",
        "path": "/data/openpilot",
        "casync": {
          "caibx": caibx_file.name,
        }
    }]
    self.release_manifests[release] += create_virtual_agnos_manifest(self.mock_update_path, self.MOCK_RELEASES[release][1])

  def setup_remote_release(self, release):
    self.update_remote_release(release)

  def setup_basedir_release(self, release):
    super().setup_basedir_release(release)
    update_release(self.basedir, release, *self.MOCK_RELEASES[release])
    create_casync_files(self.basedir, release, *self.MOCK_RELEASES[release])

  @contextlib.contextmanager
  def additional_context(self):
    self.directory_handler = DirectoryHttpServer(self.casync_dir)
    with http_server_context(self.directory_handler) as (casync_host, casync_port):
      casync_base = f"http://{casync_host}:{casync_port}"

      self.api_handler = OpenpilotChannelMockAPI(self.release_manifests, self.MOCK_RELEASES, casync_base)
      with http_server_context(self.api_handler) as (api_host, api_port):
        os.environ["API_HOST"] = f"http://{api_host}:{api_port}"

        def get_partition_path(entry: dict, target: bool):
          if entry["name"] == "system":
            return self.system_b if target else self.system_a
          if entry["name"] == "boot":
            return self.boot_b if target else self.boot_a

        with mock.patch("openpilot.system.hardware.HARDWARE.get_partition_path", get_partition_path):
          yield

  def setup_git_basedir_release(self, release):
    super().setup_basedir_release(release)
    run(["git", "init"], cwd=self.basedir)
    run(["git", "config", "user.name", "'tester'"], cwd=self.basedir)
    run(["git", "config", "user.email", "'tester@comma.ai'"], cwd=self.basedir)
    run(["git", "checkout", "-b", release], cwd=self.basedir)
    update_release(self.basedir, release, *self.MOCK_RELEASES[release])
    run(["git", "add", "."], cwd=self.basedir)
    run(["git", "commit", "-m", f"openpilot release {release}"], cwd=self.basedir)

  def _wait_for_finalized(self):
    self.wait_for_condition(lambda: get_consistent_flag(self.staging_root / "finalized"))

  def _test_channel_param(self, channel):
    self.assertEqual(self.params.get("UpdaterTargetChannel", encoding="utf-8"), channel)

  def test_no_update(self):
    # Start on release3, ensure we don't fetch any updates
    self.setup_remote_release("release3")
    self.setup_basedir_release("release3")

    with self.additional_context(), processes_context(["updated"]):
      time.sleep(5)
      self.assertEqual(get_consistent_flag(self.staging_root / "finalized"), False)

  def test_new_release(self):
    # Start on release3, simulate a release3 commit, ensure we fetch that update properly
    self.setup_remote_release("release3")
    self.setup_basedir_release("release3")

    self.MOCK_RELEASES["release3"] = ("0.1.3", "1.2", "0.1.3 release notes")
    self.update_remote_release("release3")

    with self.additional_context(), processes_context(["updated"]):
      time.sleep(5)
      self._wait_for_finalized()
      self._test_finalized_update("release3", *self.MOCK_RELEASES["release3"])

  def test_recover_from_git_update(self):
    # starts off on a git update, ensures we can recover and install the correct update
    self.setup_git_basedir_release("release3")
    self.setup_remote_release("release3")

    with self.additional_context(), processes_context(["updated"]):
      self._wait_for_finalized()
      self._test_finalized_update("release3", *self.MOCK_RELEASES["release3"])

  def test_channel_migration(self):
    # Start on 'test', remote has migrated this to 'release3', ensure we also switch to this branch
    self.MOCK_RELEASES["test"] = self.MOCK_RELEASES["release3"]
    self.setup_remote_release("release3")
    self.setup_remote_release("test")
    self.release_manifests["test"] = self.release_manifests["release3"]

    self.setup_git_basedir_release("test")
    self.setup_remote_release("release3")

    with self.additional_context(), processes_context(["updated"]):
      self._wait_for_finalized()
      self._test_finalized_update("release3", *self.MOCK_RELEASES["release3"])

  def test_recover_from_bad_api_response(self):
    # tests recovery from a bad api response
    self.setup_remote_release("release3")
    self.setup_basedir_release("release3")

    self.MOCK_RELEASES["release3"] = ("0.1.3", "1.2", "0.1.3 release notes")
    self.update_remote_release("release3")

    with self.additional_context():
      self.api_handler.API_NO_RESPONSE = True
      with processes_context(["updated"]):
        time.sleep(3)
        self.api_handler.API_NO_RESPONSE = False

        self._wait_for_finalized()
        self._test_finalized_update("release3", *self.MOCK_RELEASES["release3"])

  def test_recover_from_network_failure(self):
    # tests recovery from a network error on the directory
    self.setup_remote_release("release3")
    self.setup_basedir_release("release3")

    self.MOCK_RELEASES["release3"] = ("0.1.3", "1.2", "0.1.3 release notes")
    self.update_remote_release("release3")

    with self.additional_context():
      self.directory_handler.API_NO_RESPONSE = True
      with processes_context(["updated"]):
        time.sleep(3)
        self.directory_handler.API_NO_RESPONSE = False

        self._wait_for_finalized()
        self._test_finalized_update("release3", *self.MOCK_RELEASES["release3"])

  def test_agnos_update(self):
    self.setup_remote_release("release3")
    self.setup_basedir_release("release3")

    self.MOCK_RELEASES["release3"] = ("0.1.3", "1.3", "0.1.3 release notes")
    self.update_remote_release("release3")

    with self.additional_context(), \
      processes_context(["updated"]):

      time.sleep(1)
      self._wait_for_finalized()
      self._test_finalized_update("release3", *self.MOCK_RELEASES["release3"])

      # ensure update was pushed into correct slot
      self.assertEqual(self.boot_a.read_bytes()[:3], b"1.2")
      self.assertEqual(self.boot_b.read_bytes()[:3], b"1.3")

      self.assertEqual(self.system_a.read_bytes()[:3], b"1.2")
      self.assertEqual(self.system_b.read_bytes()[:3], b"1.3")

      self.assertEqual(len(self.system_b.read_bytes()), 3)
