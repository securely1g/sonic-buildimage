#!/usr/bin/env python3
"""Real confined-file collection and fixed query-only routing checks."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS
import yaml
from tools.bazel.ci import registry_native_collect as collect
from tools.bazel.ci import registry_native_worker as worker
from tools.bazel.tests.registry_native_worker_test import condition

ROOT=Path(__file__).resolve().parents[3]
PROFILE={"name":"two_actions","status":"pilot","minimum_memory_bytes":21474836480,"minimum_cpu":4,
         "measured_memory_limit_bytes":21474836480,"measured_cpu_limit":4.0,"jvm_heap_gib":3,
         "jobs":2,"cpu":2,"scheduler_memory_mb":8000,"loaded_container_memory_gib":4}


class CollectionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.work=self.root/"work";self.base=self.work/"bazel/base"
        self.consumer=self.work/"consumer";self.consumer.mkdir(parents=True)
        self.external=self.base/"external/sonic-dash+";self.external.mkdir(parents=True)
        for name in ["MODULE.bazel","MODULE.bazel.lock","BUILD.bazel",".bazelversion"]:
            (self.consumer/name).write_text("original "+name)
        (self.external/"MODULE.bazel").write_text("source")
        (self.external/"BUILD.bazel").write_text("never execute an arbitrary Python helper")
        self.execroot=self.base/"execroot/_main";self.out=self.execroot/"bazel-out/k8-fastbuild/bin/external/sonic-dash+"
        self.out.mkdir(parents=True);(self.out/"runtime.tar").write_bytes(b"original runtime")
        self.tree=self.out/"sdk";self.tree.mkdir();(self.tree/"header.h").write_bytes(b"header")
        (self.tree/"lib.so").symlink_to("header.h")
        self.label=collect.BUILDS[0]
        self.spec={"work":str(self.work),"config":{"build_targets":collect.BUILDS},
                   "profile":PROFILE,"downloader_config":str(self.root/"mirrors"),
                   "distdirs":[str(self.root/"distdir")],"repository_cache":str(self.root/"repository"),
                   "disk_cache":str(self.root/"disk")}

    def row(self):
        return {"label":self.label,"resolved_label":"@@sonic-dash+//:runtime",
                "files":[{"path":(self.out/"runtime.tar").relative_to(self.execroot).as_posix(),"tree":False},
                         {"path":self.tree.relative_to(self.execroot).as_posix(),"tree":True}]}

    def test_original_configuration_query_only_no_fetch_or_lock_update(self):
        argv=collect.query_command(Path("/fixed/pinned/bazel"),self.spec,self.base,Path("/actions/_work/registry"),self.label)
        self.assertIn("cquery",argv);self.assertNotIn("build",argv);self.assertNotIn("test",argv)
        self.assertIn("--nofetch",argv);self.assertIn("--lockfile_mode=error",argv)
        self.assertNotIn("--lockfile_mode=update",argv)
        self.assertIn("--output_base="+str(self.base),argv)
        self.assertIn("--output_user_root="+str(self.work/"bazel"),argv)
        self.assertIn("--ignore_all_rc_files",argv)
        self.assertEqual(argv[-1],"config("+self.label+", target)")
        self.assertIn("--remote_cache=",argv)
        self.assertIn("--host_platform=@sonic-build-infra//platforms:x86_64_trixie",argv)
        for bad in ["@other//:runtime","//:all"]:
            with self.assertRaises(ValueError):collect.query_command(Path("/bazel"),self.spec,self.base,self.consumer,bad)
        changed=copy.deepcopy(self.spec);changed["profile"]["jobs"]=3
        with self.assertRaises(ValueError):collect.query_command(Path("/bazel"),changed,self.base,self.consumer,self.label)

    def test_only_existing_default_outputs_and_exact_target(self):
        row=self.row()
        self.assertEqual(collect.parse_query(json.dumps(row),self.label,self.execroot),row)
        for case in ["duplicate","traversal","absolute","wrong-label","boolean-type","missing"]:
            bad=copy.deepcopy(row)
            if case=="duplicate":bad["files"].append(bad["files"][0])
            elif case=="traversal":bad["files"][0]["path"]="bazel-out/../../secret"
            elif case=="absolute":bad["files"][0]["path"]="/etc/passwd"
            elif case=="wrong-label":bad["label"]="@other//:runtime"
            elif case=="boolean-type":bad["files"][0]["tree"]=0
            else:bad["files"][0]["path"]="bazel-out/missing"
            with self.subTest(case=case),self.assertRaises(ValueError):
                collect.parse_query(json.dumps(bad),self.label,self.execroot)

    def test_output_symlink_escape_and_special_nodes_rejected(self):
        outside=self.root/"outside";outside.write_text("outside")
        (self.tree/"escape").symlink_to(outside)
        with self.assertRaisesRegex(ValueError,"escapes"):collect.output_snapshot([self.row()],self.execroot)
        (self.tree/"escape").unlink()
        os.mkfifo(self.tree/"fifo")
        with self.assertRaisesRegex(ValueError,"Special"):collect.output_snapshot([self.row()],self.execroot)

    def test_over_budget_payload_rejects_before_digest_and_tree_links_stay_within_tree(self):
        path=self.out/"runtime.tar"
        with path.open("wb") as stream: stream.truncate(8*1024**3+1)
        with patch.object(collect,"stable_file") as digest:
            with self.assertRaisesRegex(ValueError,"before hashing"):
                collect.output_snapshot([self.row()],self.execroot)
            digest.assert_not_called()
        path.write_bytes(b"original runtime")
        (self.tree/"outside-tree").symlink_to("../runtime.tar")
        with self.assertRaisesRegex(ValueError,"escapes"):
            collect.output_snapshot([self.row()],self.execroot)

    def test_real_archive_binds_tree_members_and_source_is_unchanged(self):
        row=self.row();before=collect.output_snapshot([row],self.execroot)
        dest=self.root/"new"/"outputs";dest.parent.mkdir()
        with patch.object(shutil,"disk_usage",return_value=NS(free=10**12)):
            index=collect.archive_outputs([row],self.execroot,dest,before)
        self.assertEqual(collect.output_snapshot([row],self.execroot),before)
        self.assertEqual(len(index[self.label]),2)
        for item in index[self.label]:
            target=dest.parent/item["path"]
            self.assertEqual(worker.digest(target),item["sha256"])
            self.assertEqual(target.stat().st_size,item["bytes"])
            self.assertEqual(target.stat().st_mode & 0o777,0o644)
            if item["tree"]:
                with tarfile.open(target) as archive:
                    self.assertEqual(archive.extractfile("header.h").read(),b"header")
                    self.assertEqual(archive.getmember("lib.so").linkname,"header.h")
            else:self.assertEqual(target.read_bytes(),b"original runtime")
        with self.assertRaises(FileExistsError):collect.archive_outputs([row],self.execroot,dest,before)

    def test_modified_bytes_and_low_capacity_do_not_claim_collection(self):
        row=self.row();before=collect.output_snapshot([row],self.execroot)
        (self.tree/"header.h").write_bytes(b"tampered")
        with patch.object(shutil,"disk_usage",return_value=NS(free=10**12)),self.assertRaisesRegex(ValueError,"changed"):
            collect.archive_outputs([row],self.execroot,self.root/"changed",before)
        with patch.object(shutil,"disk_usage",return_value=NS(free=1)),self.assertRaisesRegex(ValueError,"capacity"):
            collect.archive_outputs([row],self.execroot,self.root/"full",before)

    def test_source_fence_detects_lock_and_starlark_changes(self):
        before=collect.source_fence(self.work,self.base)
        (self.consumer/"MODULE.bazel.lock").write_text("changed")
        self.assertNotEqual(collect.source_fence(self.work,self.base),before)
        (self.external/"new.bzl").write_text("new repository definition")
        self.assertIn("bazel/base/external/sonic-dash+/new.bzl",collect.source_fence(self.work,self.base))
        (self.external/"escape.bzl").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError,"escapes"):collect.source_fence(self.work,self.base)

    def test_pinned_binary_rejects_unverified_cached_launcher(self):
        cache=self.root/"cache";path=cache/"bazelisk/downloads/sha256"/collect.BAZEL_SHA256/"bin/bazel"
        path.parent.mkdir(parents=True);path.write_bytes(b"do not execute")
        with patch.object(subprocess,"run") as commands,patch.object(subprocess,"check_output") as checks:
            with self.assertRaisesRegex(ValueError,"unavailable"):collect.query_binary(cache)
            commands.assert_not_called();checks.assert_not_called()

    def test_query_shutdown_uses_exact_consumer_and_outputbase_and_rejects_live_pid(self):
        server=self.base/"server/server.pid.txt";server.parent.mkdir();server.write_text("123")
        before={"state":"S","rss_pages":10,"started":99}
        calls=[]
        def run(argv,**kwargs):
            calls.append((argv,kwargs));return NS(returncode=0)
        with patch.object(worker,"server_process",side_effect=[before,None]),patch.object(subprocess,"run",side_effect=run):
            value=collect.stop_query(Path("/fixed/bazel"),self.spec,self.base,self.consumer)
        self.assertEqual(value["status"],"stopped")
        self.assertEqual(calls[0][0][-1],"shutdown")
        self.assertIn("--output_base="+str(self.base),calls[0][0])
        self.assertEqual(calls[0][1]["cwd"],self.consumer)
        with patch.object(worker,"server_process",return_value=before),patch.object(subprocess,"run",side_effect=run):
            with self.assertRaisesRegex(ValueError,"remains live"):collect.stop_query(Path("/fixed/bazel"),self.spec,self.base,self.consumer)
        server.unlink()
        with patch.object(subprocess,"run") as commands:
            self.assertEqual(collect.stop_query(Path("/fixed/bazel"),self.spec,self.base,self.consumer)["status"],"not_started")
            commands.assert_not_called()

    def test_exact_query_process_is_observed_stopped(self):
        process=subprocess.Popen([__import__("sys").executable,"-c","import time; time.sleep(60)"])
        self.addCleanup(lambda: process.poll() is None and process.kill())
        server=self.base/"server/server.pid.txt";server.parent.mkdir();server.write_text(str(process.pid))
        observed=worker.server_process(process.pid);self.assertIsNotNone(observed)
        def shutdown(argv,**kwargs):
            self.assertEqual(argv[-1],"shutdown")
            self.assertEqual(kwargs["cwd"],self.consumer)
            self.assertIn("--output_base="+str(self.base),argv)
            process.terminate();process.wait(timeout=5)
            return NS(returncode=0)
        with patch.object(subprocess,"run",side_effect=shutdown):
            result=collect.stop_query(Path("/fixed/bazel"),self.spec,self.base,self.consumer)
        self.assertEqual(result["pid"],process.pid)
        self.assertEqual(result["before"]["started"],observed["started"])
        self.assertIsNone(result["after"])

    def test_source_inventory_identifies_real_link_without_reading_payloads(self):
        target=self.external/"actual.bzl";target.write_text("do not execute")
        link=self.external/"alias.bzl";link.symlink_to("actual.bzl")
        outside=self.root/"private";outside.write_text("never read this")
        (self.external/"outside.bzl").symlink_to(outside)
        (self.external/"loop.bzl").symlink_to("loop.bzl")
        os.mkfifo(self.external/"special.bzl")
        directory=self.external/"linked-directory";directory.symlink_to(self.root,target_is_directory=True)
        with patch.object(collect,"stable_file") as hashing,patch.object(subprocess,"run") as commands:
            result=collect.source_inventory(self.work,self.base)
            hashing.assert_not_called();commands.assert_not_called()
        records=result["records"];prefix="bazel/base/external/sonic-dash+/"
        self.assertEqual(result["first_nonregular"],prefix+"alias.bzl")
        self.assertTrue(records[prefix+"alias.bzl"]["target_within_original_work"])
        self.assertTrue(records[prefix+"alias.bzl"]["target_is_regular"])
        self.assertEqual(records[prefix+"outside.bzl"]["target_within_original_work"],False)
        self.assertNotIn("resolved_relative_path",records[prefix+"outside.bzl"])
        self.assertIn("resolution_error_class",records[prefix+"loop.bzl"])
        self.assertEqual(records[prefix+"special.bzl"]["kind"],"fifo")
        self.assertFalse(result["file_payloads_read"])
        self.assertFalse(result["configured_query_executed"])
        self.assertFalse(any("linked-directory/" in name for name in records))

    def test_failure_sidecars_preserve_root_record_for_outer_uid(self):
        root=self.root/collect.failure_receipt_name(0);root.write_text("original root failure");root.chmod(0o444)
        outer=self.root/collect.failure_receipt_name(1000);outer.write_text("outer failure")
        self.assertNotEqual(root,outer)
        self.assertEqual(root.read_text(),"original root failure")
        self.assertEqual(root.stat().st_mode & 0o777,0o444)

    def test_source_inventory_dispatch_selects_no_query_step(self):
        jobs=yaml.safe_load((ROOT/".github/workflows/bazel-oci.yml").read_text())["jobs"]
        ctx={"mode":"registry-native-source-inventory","consumer":"dash"}
        self.assertEqual([name for name,body in jobs.items() if condition(str(body.get("if","true")),**ctx)],
                         ["registry_native_export"])
        job=jobs["registry_native_export"]
        step=[x for x in job["steps"] if "registry_native_collect.py source-inventory" in x.get("run","")]
        self.assertEqual(len(step),1)
        self.assertEqual(step[0]["if"],"inputs.source_image == 'registry-native-source-inventory'")
        self.assertEqual([x["if"] for x in job["steps"] if x.get("id")=="collect"],
                         ["inputs.source_image == 'registry-native-collect'"])
        for line in step[0]["run"].splitlines()[:-1]:self.assertTrue(line.endswith(chr(92)))

    def test_workflow_routes_only_fixed_collect_step_and_preserves_other_modes(self):
        jobs=yaml.safe_load((ROOT/".github/workflows/bazel-oci.yml").read_text())["jobs"]
        ctx={"mode":"registry-native-collect","consumer":"dash"}
        self.assertEqual([name for name,body in jobs.items() if condition(str(body.get("if","true")),**ctx)],
                         ["registry_native_export"])
        job=jobs["registry_native_export"];self.assertNotIn("needs",job)
        step=[x for x in job["steps"] if x.get("id")=="collect"];self.assertEqual(len(step),1)
        self.assertEqual(step[0]["if"],"inputs.source_image == 'registry-native-collect'")
        for bad in [{"event":"pull_request"},{"actor":"other"},{"consumer":"fips"},
                    {"worker_name":"sonic-slave-lgh114-1"},{"revision":"0"*40}]:
            self.assertFalse(condition(job["if"],**{**ctx,**bad}))
        source=(ROOT/"tools/bazel/ci/registry_native_collect.py").read_text()
        for prohibited in ["prepare_tools(", "runner.run(", "runner.collect_outputs(", "bazel-wrapper"]:
            self.assertNotIn(prohibited,source)


if __name__=="__main__":
    unittest.main()
