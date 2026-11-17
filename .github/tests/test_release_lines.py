"""Offline GitHub model: all release-line API calls stay inside this process."""

import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import release_line_checks as checks
import propose_release_line as propose


class GitHub:
    coordinator = "linuxdeepin/dde"
    line = "develop-2500u1"
    branch = "develop/2500u1"
    actions_id = 15368
    app_id = 174141

    def __init__(self):
        self.serial = 100
        self.cfg = yaml.safe_load((ROOT / "groups.yaml").read_text())
        self.cfg["release_lines"] = {"master": {"member_branch": "master"}}
        self.cfg["groups"]["G2"]["versions"] = {"master": "6.1.113"}
        self.cfg["groups"]["G2"].pop("version", None)
        self.members = checks.g2_members(self.cfg, "linuxdeepin")
        template_root = ROOT.parent / ".github@verify-group-version"
        self.templates = {name: (template_root / "workflow-templates" / name).read_text()
                          for name in checks.WORKFLOW_FILES}
        self.refs, self.commits, self.files, self.trees = {}, {}, {}, {}
        self.protections, self.rules, self.rulesets, self.creation_rules = {}, {}, {}, {}
        self.prs, self.calls, self.writes, self.fail_once = [], [], [], {}
        self.workflows, self.enabled, self.repo_changes = {}, {}, {}
        self.drop_check = None
        self.add_commit(self.coordinator, {"groups.yaml": yaml.safe_dump(self.cfg, sort_keys=False)},
                        branch="master")
        self.shared_repo = "linuxdeepin/.github"
        self.add_commit(self.shared_repo, {
            f".github/workflows/{name}": (template_root / ".github/workflows" / name).read_text()
            for name in ("update-changelog.yml", "verify-group-version.yml")}, branch="master")
        self.bases = {}
        for index, (member, repo) in enumerate(self.members):
            version = "1:6.1.113" if index == 0 else "6.1.113"
            files = {f".github/workflows/{name}": text for name, text in self.templates.items()}
            files["debian/changelog"] = self.changelog(version)
            self.bases[member] = self.add_commit(repo, files, branch="master")
            self.protections[repo, "master"] = self.policy()

    def new_sha(self):
        self.serial += 1
        return f"{self.serial:040x}"

    @staticmethod
    def changelog(version):
        return (f"example ({version}) unstable; urgency=medium\n\n  * Baseline.\n\n"
                " -- Example Maintainer <example@example.com>  Tue, 17 Nov 2026 08:00:00 +0800\n")

    def policy(self):
        return {
            "required_status_checks": {"strict": True, "contexts": ["unit-test", checks.GUARD_CONTEXT],
                                      "checks": [{"context": "unit-test", "app_id": 111},
                                                 {"context": checks.GUARD_CONTEXT, "app_id": self.actions_id}]},
            "required_pull_request_reviews": {
                "required_approving_review_count": 2, "dismiss_stale_reviews": True,
                "require_code_owner_reviews": True, "require_last_push_approval": True,
                "dismissal_restrictions": {"users": [{"login": "maintainer"}], "teams": [], "apps": []},
                "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": []}},
            "restrictions": {"users": [], "teams": [{"slug": "desktop"}], "apps": []},
            "enforce_admins": {"enabled": True}, "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": False}, "required_linear_history": {"enabled": False},
            "required_conversation_resolution": {"enabled": True}, "lock_branch": {"enabled": False},
        }

    def add_commit(self, repo, files, parents=(), branch=None, message="commit"):
        sha, tree = self.new_sha(), self.new_sha()
        self.files[repo, sha] = copy.deepcopy(files)
        self.trees[repo, tree] = copy.deepcopy(files)
        self.commits[repo, sha] = {"sha": sha, "tree": {"sha": tree}, "message": message,
                                  "parents": [{"sha": p} for p in parents]}
        if branch:
            self.set_ref(repo, branch, sha)
        return sha

    def set_ref(self, repo, branch, sha):
        self.refs[repo, branch] = sha
        for pr in self.prs:
            if pr["repo"] == repo and pr["head"]["ref"] == branch and pr["state"] == "open":
                pr["head"]["sha"] = sha

    def ancestors(self, repo, sha):
        result, pending = set(), [sha]
        while pending:
            sha = pending.pop()
            if sha in result:
                continue
            result.add(sha)
            pending.extend(p["sha"] for p in self.commits[repo, sha]["parents"])
        return result

    def add_pr(self, repo, head, base="master", draft=True, body="", title="test"):
        pr = {"repo": repo, "number": len(self.prs) + 1, "state": "open", "draft": draft,
              "created_at": f"2026-11-17T08:{len(self.prs):02}:00Z", "merged_at": None,
              "merge_commit_sha": None, "body": body, "title": title,
              "head": {"ref": head, "sha": self.refs[repo, head], "repo": {"full_name": repo}},
              "base": {"ref": base, "sha": self.refs[repo, base]}}
        pr["html_url"] = f"https://github.com/{repo}/pull/{pr['number']}"
        self.prs.append(pr)
        return pr

    def merge(self, pr):
        repo, base, head = pr["repo"], pr["base"]["ref"], pr["head"]["sha"]
        files = copy.deepcopy(self.files[repo, self.refs[repo, base]])
        merge_base = max(self.ancestors(repo, self.refs[repo, base]) & self.ancestors(repo, head))
        for name in set(self.files[repo, merge_base]) | set(self.files[repo, head]):
            if self.files[repo, merge_base].get(name) != self.files[repo, head].get(name):
                if name in self.files[repo, head]:
                    files[name] = self.files[repo, head][name]
                else:
                    files.pop(name, None)
        sha = self.add_commit(repo, files, [self.refs[repo, base], head], branch=base)
        pr.update(state="closed", merged_at="2026-11-17T10:00:00Z", merge_commit_sha=sha)
        return sha

    def with_line(self):
        self.cfg["release_lines"][self.line] = {"member_branch": self.branch, "bases": copy.deepcopy(self.bases)}
        self.cfg["groups"]["G2"]["versions"][self.line] = "6.1.113"
        old = self.refs[self.coordinator, "master"]
        return self.add_commit(self.coordinator, {"groups.yaml": yaml.safe_dump(self.cfg, sort_keys=False)},
                               [old], branch="master")

    def target(self, repo, protected=True, sha=None):
        self.set_ref(repo, self.branch, sha or self.refs[repo, "master"])
        if protected:
            self.protections[repo, self.branch] = self.policy()

    def api(self, url, data=None, method=None, missing=False, paginate=False):
        parsed = urlsplit(url)
        path, query = unquote(parsed.path), parse_qs(parsed.query)
        self.calls.append((path, copy.deepcopy(data), method))
        if path != "graphql" and (data is not None or method and method != "GET"):
            self.writes.append((path, copy.deepcopy(data), method))
        if path in self.fail_once:
            error = self.fail_once.pop(path)
            if callable(error):
                return error()
            raise error
        if path == "graphql":
            repo = data["variables"]["owner"] + "/" + data["variables"]["name"]
            return {"data": {"repository": {"branchProtectionRules": {
                "nodes": copy.deepcopy(self.creation_rules.get(repo, [])),
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        if path == "apps/github-actions":
            return {"id": self.actions_id, "slug": "github-actions", "name": "GitHub Actions"}
        parts = path.split("/")
        if parts[0] != "repos":
            raise AssertionError(url)
        repo, suffix = "/".join(parts[1:3]), "/".join(parts[3:])
        if not suffix:
            return {"default_branch": "master", "archived": False, "disabled": False,
                    "permissions": {"push": True}, **self.repo_changes.get(repo, {})}
        if suffix.startswith("git/ref/heads/"):
            branch = suffix[len("git/ref/heads/"):]
            sha = self.refs.get((repo, branch))
            return {"object": {"sha": sha}} if sha else None
        if suffix == "git/refs" and data is not None:
            branch = data["ref"].removeprefix("refs/heads/")
            if (repo, branch) in self.refs:
                raise RuntimeError("reference already exists (HTTP 422)")
            self.set_ref(repo, branch, data["sha"])
            return {"object": {"sha": data["sha"]}}
        if suffix.startswith("git/refs/heads/") and method == "PATCH":
            branch = suffix[len("git/refs/heads/"):]
            assert data["force"] is False
            if self.refs[repo, branch] not in self.ancestors(repo, data["sha"]):
                raise RuntimeError("not fast-forward")
            self.set_ref(repo, branch, data["sha"])
            return {}
        if suffix.startswith("git/commits/"):
            sha = suffix[len("git/commits/"):]
            if (repo, sha) not in self.commits:
                raise RuntimeError("commit not found (HTTP 404)")
            return copy.deepcopy(self.commits[repo, sha])
        if suffix == "git/trees" and data is not None:
            tree = self.new_sha()
            files = copy.deepcopy(self.trees[repo, data["base_tree"]])
            for file in data["tree"]:
                files[file["path"]] = file["content"]
            self.trees[repo, tree] = files
            return {"sha": tree}
        if suffix == "git/commits" and data is not None:
            sha = self.add_commit(repo, self.trees[repo, data["tree"]], data["parents"],
                                  message=data["message"])
            return copy.deepcopy(self.commits[repo, sha])
        if suffix.startswith("contents/"):
            name = suffix[len("contents/"):]
            ref = query["ref"][0]
            sha = self.refs.get((repo, ref), ref)
            text = self.files.get((repo, sha), {}).get(name)
            if text is None:
                if missing:
                    return None
                raise RuntimeError("file missing (HTTP 404)")
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if suffix.startswith("compare/"):
            a, b = suffix[len("compare/"):].split("...")
            a, b = self.refs.get((repo, a), a), self.refs.get((repo, b), b)
            ancestors_a, ancestors_b = self.ancestors(repo, a), self.ancestors(repo, b)
            common = a if a in ancestors_b else b if b in ancestors_a else max(
                ancestors_a & ancestors_b, default=None)
            if common is None:
                raise RuntimeError("no common ancestor (HTTP 404)")
            before, after = self.files[repo, common], self.files[repo, b]
            files = [{"filename": name, "status": "added" if name not in before else
                      "removed" if name not in after else "modified"}
                     for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
            return {"merge_base_commit": {"sha": common}, "files": files}
        if suffix.startswith("branches/"):
            branch = suffix[len("branches/"):]
            if branch.endswith("/protection"):
                branch = branch[:-len("/protection")]
                if method == "PUT":
                    protection = copy.deepcopy(data)
                    for name in ("enforce_admins", "allow_force_pushes", "allow_deletions",
                                 "lock_branch", "required_linear_history", "required_conversation_resolution"):
                        if name in protection:
                            protection[name] = {"enabled": protection[name]}
                    if self.drop_check:
                        protection["required_status_checks"]["checks"] = [
                            check for check in protection["required_status_checks"]["checks"]
                            if check["context"] != self.drop_check]
                    self.protections[repo, branch] = protection
                return copy.deepcopy(self.protections.get((repo, branch)))
            sha = self.refs.get((repo, branch))
            if not sha:
                if missing:
                    return None
                raise RuntimeError("branch missing (HTTP 404)")
            return {"name": branch, "commit": {"sha": sha},
                    "protected": (repo, branch) in self.protections or bool(self.rules.get((repo, branch)))}
        if suffix.startswith("rules/branches/"):
            return copy.deepcopy(self.rules.get((repo, suffix[len("rules/branches/"):]), []))
        if suffix.startswith("rulesets/"):
            return copy.deepcopy(self.rulesets[repo, int(suffix[len("rulesets/"):])])
        if suffix == "actions/permissions":
            return {"enabled": self.enabled.get(repo, True)}
        if suffix.startswith("actions/workflows/"):
            name = suffix[len("actions/workflows/"):]
            if name.endswith("/enable"):
                name = name[:-len("/enable")]
                assert method == "PUT"
                self.workflows[repo, name] = "active"
                return None
            state = self.workflows.get((repo, name), "active")
            return {"state": state} if state else None
        if suffix == "pulls":
            if data is not None:
                return copy.deepcopy(self.add_pr(repo, data["head"], data["base"],
                                                 data.get("draft", False), data.get("body", ""),
                                                 data.get("title", "")))
            result = [pr for pr in self.prs if pr["repo"] == repo]
            if query.get("state") != ["all"]:
                result = [pr for pr in result if pr["state"] == query["state"][0]]
            if "head" in query:
                branch = query["head"][0].split(":", 1)[1]
                result = [pr for pr in result if pr["head"]["ref"] == branch]
            if "base" in query:
                result = [pr for pr in result if pr["base"]["ref"] == query["base"][0]]
            return copy.deepcopy(list(reversed(result)))
        if suffix.startswith("pulls/") and data is not None:
            pr = next(pr for pr in self.prs if pr["repo"] == repo and pr["number"] == int(parts[-1]))
            pr.update(data)
            return copy.deepcopy(pr)
        raise AssertionError((url, data, method))


class ChecksTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub()
        self.repo = self.github.members[0][1]
        self.github.target(self.repo)

    def ready(self):
        return checks.check_member_ready(self.github.api, self.repo, self.github.branch)

    def test_ready_only_reads_and_uses_actual_actions_app_binding(self):
        self.assertEqual(self.ready()["state"], "READY")
        self.assertEqual(self.github.writes, [])
        self.assertIn(("apps/github-actions", None, None), self.github.calls)

    def test_independent_ids_multilevel_branches_and_unique_mapping(self):
        checks.validate_line("develop-2500u1", "develop/releases/old-u1")
        checks.validate_line("stable.u1", "develop/2500u1")
        for line, branch in (("bad/id", "develop/u1"), ("u1", "master"), ("u1", "develop/../x")):
            with self.subTest(line=line, branch=branch), self.assertRaises(RuntimeError):
                checks.validate_line(line, branch)
        cfg = copy.deepcopy(self.github.cfg)
        cfg["release_lines"]["other"] = {"member_branch": "master"}
        cfg["groups"]["G2"]["versions"]["other"] = "6.1.113"
        with self.assertRaises(RuntimeError):
            checks.release_lines(cfg)

    def test_missing_inactive_or_disabled_workflow_never_ready(self):
        sha = self.github.refs[self.repo, self.github.branch]
        del self.github.files[self.repo, sha][".github/workflows/call-update-changelog.yml"]
        with self.assertRaisesRegex(RuntimeError, "TEMPLATE_MISSING"):
            self.ready()
        self.github.files[self.repo, sha][".github/workflows/call-update-changelog.yml"] = self.github.templates[
            "call-update-changelog.yml"]
        self.github.workflows[self.repo, "call-update-changelog.yml"] = "disabled_manually"
        with self.assertRaisesRegex(RuntimeError, "WORKFLOW_INACTIVE"):
            self.ready()
        self.github.workflows.clear()
        self.github.enabled[self.repo] = False
        with self.assertRaisesRegex(RuntimeError, "WORKFLOW_INACTIVE"):
            self.ready()

    def test_input_contract_and_forwarded_base_branch_are_required(self):
        name = checks.WORKFLOW_FILES[0]
        for change in ("type", "forward", "extra"):
            workflow = yaml.safe_load(self.github.templates[name])
            if change == "type":
                workflow[True]["workflow_dispatch"]["inputs"]["base_branch"]["type"] = "boolean"
            elif change == "forward":
                workflow["jobs"]["update_changelog"]["with"]["base_branch"] = "master"
            else:
                workflow[True]["workflow_dispatch"]["inputs"]["extra"] = {"type": "string", "required": True}
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, "TEMPLATE_MISSING"):
                checks.check_workflow_file(self.github.api, self.repo, name, "template",
                                           self.github.branch, text=yaml.safe_dump(workflow))

    def test_guard_filters_cover_all_branch_prs(self):
        name = checks.WORKFLOW_FILES[1]
        for patterns, accepted in ((["master"], False), (["*"], False),
                                   (["develop/*"], True), (["develop/**"], True),
                                   (["develop/**", "!develop/2500u1"], False)):
            workflow = yaml.safe_load(self.github.templates[name])
            workflow[True]["pull_request_target"]["branches"] = patterns
            with self.subTest(patterns=patterns):
                if accepted:
                    checks.check_workflow_file(self.github.api, self.repo, name, "template",
                                               self.github.branch, text=yaml.safe_dump(workflow))
                else:
                    with self.assertRaises(RuntimeError):
                        checks.check_workflow_file(self.github.api, self.repo, name, "template",
                                                   self.github.branch, text=yaml.safe_dump(workflow))
        self.assertFalse(checks._matches("develop?2500u1", "develop/2500u1"))

    def test_shared_reference_and_read_errors_fail_closed(self):
        self.github.fail_once["repos/linuxdeepin/.github/contents/.github/workflows/update-changelog.yml"] = (
            RuntimeError("permission unknown (HTTP 403)"))
        with self.assertRaisesRegex(RuntimeError, "permission unknown"):
            self.ready()
        self.github.fail_once[f"repos/{self.repo}/branches/develop/2500u1/protection"] = (
            RuntimeError("protection API unavailable (HTTP 403)"))
        with self.assertRaisesRegex(RuntimeError, "protection API unavailable"):
            self.ready()

    def test_unsupported_guard_globs_and_both_branch_filters_fail_closed(self):
        for event, branch in (
            ({"branches": ["develop/**"], "branches-ignore": ["master"]}, "develop/u1"),
            ({"branches": ["develop/u+"]}, "develop/u+"),
            ({"branches": [r"develop/\*"]}, "develop/*"),
            ({"branches-ignore": ["develop/u+"]}, "develop/u1"),
        ):
            with self.subTest(event=event):
                self.assertFalse(checks._covers(event, branch))

    def test_malformed_api_response_preserves_runtime_error_contract(self):
        self.github.fail_once[f"repos/{self.repo}/branches/develop/2500u1"] = (
            lambda: {"commit": None, "protected": True})
        with self.assertRaises(checks.ReadinessError) as failure:
            self.ready()
        self.assertIsInstance(failure.exception, RuntimeError)
        self.assertIsInstance(failure.exception.__cause__, TypeError)

    def test_each_classic_protection_requirement_is_enforced(self):
        for field in ("strict", "app_id", "review", "admin", "force", "delete", "bypass", "lock"):
            protection = self.github.policy()
            if field == "strict":
                protection["required_status_checks"]["strict"] = False
            elif field == "app_id":
                protection["required_status_checks"]["checks"][1]["app_id"] = self.github.app_id
            elif field == "review":
                protection["required_pull_request_reviews"] = None
            elif field == "bypass":
                protection["required_pull_request_reviews"]["bypass_pull_request_allowances"]["apps"] = ["org-app"]
            else:
                key = {"admin": "enforce_admins", "force": "allow_force_pushes",
                       "delete": "allow_deletions", "lock": "lock_branch"}[field]
                protection[key]["enabled"] = field != "admin"
            self.github.protections[self.repo, self.github.branch] = protection
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "PROTECTION_FAILED"):
                self.ready()

    def test_404_without_readable_effective_protection_never_ready(self):
        self.github.protections[self.repo, self.github.branch] = None
        with self.assertRaisesRegex(RuntimeError, "404"):
            self.ready()

    def test_active_rulesets_can_prove_protection_without_classic_endpoint(self):
        self.github.protections.pop((self.repo, self.github.branch))
        rules = [{"type": name, "ruleset_id": 1} for name in ("pull_request", "deletion", "non_fast_forward")]
        rules.append({"type": "required_status_checks", "ruleset_id": 1, "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": checks.GUARD_CONTEXT, "integration_id": self.github.actions_id}]}})
        self.github.rules[self.repo, self.github.branch] = rules
        self.github.rulesets[self.repo, 1] = {"enforcement": "active", "bypass_actors": []}
        self.assertEqual(self.ready()["state"], "READY")
        self.github.rulesets[self.repo, 1].pop("bypass_actors")
        with self.assertRaisesRegex(RuntimeError, "绕过权限"):
            self.ready()
        self.github.rulesets[self.repo, 1]["bypass_actors"] = [{"actor_type": "RepositoryRole", "actor_id": 5}]
        with self.assertRaises(RuntimeError):
            self.ready()


class ProposeTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub()

    def run_propose(self, dry_run=True, source_branch="master"):
        with contextlib.redirect_stdout(io.StringIO()):
            return propose.propose(self.github.api, self.github.coordinator,
                                   self.github.line, self.github.branch,
                                   source_branch=source_branch, dry_run=dry_run)

    def baseline(self, merged=False, deleted=False, line="master", source_branch="master"):
        old = self.github.refs[self.github.coordinator, "master"]
        self.release = {"group": "G2", "release_line": line, "base_branch": "master",
                        "base_sha": old, "version": "6.1.113", "started_at": "2026-11-17T00:00:00Z",
                        "members": [{"repo": repo, "base": source_branch,
                                     "version": "1:6.1.113" if i == 0 else "6.1.113"}
                                    for i, (_, repo) in enumerate(self.github.members)]}
        branch = f"baseline-test-formal-prs-20261113/{line}/G2"
        sha = self.github.add_commit(self.github.coordinator, self.github.files[self.github.coordinator, old],
                                     [old], branch=branch,
                                     message=propose.RELEASE_MARKER + json.dumps(self.release) + "\n-->")
        pr = self.github.add_pr(self.github.coordinator, branch)
        if merged:
            self.github.merge(pr)
        else:
            pr["state"] = "closed"
        if deleted:
            del self.github.refs[self.github.coordinator, branch]
        return sha, pr

    def member_merges(self, wrong_version=False):
        for i, (_, repo) in enumerate(self.github.members):
            old = self.github.refs[repo, "master"]
            version = self.release["members"][i]["version"]
            branch = "automation/update-changelog/" + ("1-6.1.113" if i == 0 else "6.1.113")
            files = copy.deepcopy(self.github.files[repo, old])
            files["debian/changelog"] = self.github.changelog("6.1.112" if wrong_version else version)
            self.github.add_commit(repo, files, [old], branch=branch)
            self.github.merge(self.github.add_pr(repo, branch))

    def test_dry_run_freezes_all_17_heads_and_strips_only_epoch(self):
        result = self.run_propose()
        self.assertEqual(result["bases"], self.github.bases)
        self.assertEqual(result["version"], "6.1.113")
        self.assertEqual(self.github.writes, [])
        new = yaml.safe_load(result["config"])
        self.assertEqual(new["groups"]["G1"], self.github.cfg["groups"]["G1"])
        self.assertEqual(new["repo_overrides"], self.github.cfg["repo_overrides"])
        self.assertIn("dde-session-shell-v25", new["release_lines"][self.github.line]["bases"])
        self.assertIn("linuxdeepin/dde-session-shell-snipe", [repo for _, repo in self.github.members])

    def test_bad_sha_or_mixed_version_creates_nothing(self):
        for bad in ("sha", "version"):
            github = self.github = GitHub()
            member, repo = github.members[-1]
            if bad == "sha":
                del github.commits[repo, github.bases[member]]
            else:
                github.files[repo, github.bases[member]]["debian/changelog"] = github.changelog("6.1.114")
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                self.run_propose(False)
            self.assertEqual(github.writes, [])

    def test_unregistered_source_freezes_its_heads_and_survives_source_movement(self):
        source, expected = "release/candidates/2500u3", {}
        for member, repo in self.github.members:
            files = copy.deepcopy(self.github.files[repo, self.github.bases[member]])
            files["debian/changelog"] = self.github.changelog("6.1.112")
            expected[member] = self.github.add_commit(repo, files, branch=source)
            self.assertNotIn(expected[member], self.github.ancestors(repo, self.github.refs[repo, "master"]))
        first = self.run_propose(False, source)
        cfg = yaml.safe_load(self.github.files[self.github.coordinator, first["head"]["sha"]]["groups.yaml"])
        self.assertEqual(cfg["release_lines"][self.github.line]["bases"], expected)
        self.assertEqual(cfg["groups"]["G2"]["versions"][self.github.line], "6.1.112")
        self.assertIn(f"`{source}`", first["body"])
        for member, repo in self.github.members:
            files = copy.deepcopy(self.github.files[repo, expected[member]])
            files["debian/changelog"] = self.github.changelog("6.1.114")
            self.github.add_commit(repo, files, [expected[member]], branch=source)
        self.github.calls.clear()
        self.github.writes.clear()
        second = self.run_propose(False, source)
        self.assertEqual(second["head"]["sha"], first["head"]["sha"])
        self.assertEqual(self.github.writes, [])
        self.assertFalse(any(path == f"repos/{repo}/git/ref/heads/{source}"
                             for repo in (repo for _, repo in self.github.members)
                             for path, _, _ in self.github.calls))
        with self.assertRaisesRegex(RuntimeError, "源分支或目标输入不同"):
            self.run_propose(False, "master")
        self.assertEqual(self.github.writes, [])

    def test_existing_target_can_be_its_own_unregistered_source(self):
        for _, repo in self.github.members:
            self.github.target(repo)
        before = copy.deepcopy(self.github.refs)
        pr = self.run_propose(False, self.github.branch)
        cfg = yaml.safe_load(self.github.files[self.github.coordinator, pr["head"]["sha"]]["groups.yaml"])
        self.assertEqual(cfg["release_lines"][self.github.line]["bases"], self.github.bases)
        self.assertTrue(all(self.github.refs[repo, self.github.branch] == before[repo, self.github.branch]
                            for _, repo in self.github.members))
        self.assertTrue(all(path.startswith(f"repos/{self.github.coordinator}/")
                            for path, _, _ in self.github.writes))

    def test_invalid_missing_or_mixed_source_creates_nothing(self):
        for source in ("", "develop/../candidate", "-candidate", "develop candidate", None):
            with self.subTest(source=source), self.assertRaisesRegex(RuntimeError, "非法源成员分支"):
                self.run_propose(False, source)
            self.assertEqual(self.github.calls, [])
        source = "release/candidate"
        for failure in ("missing", "version"):
            self.github = GitHub()
            for member, repo in self.github.members:
                self.github.add_commit(repo, self.github.files[repo, self.github.bases[member]], branch=source)
            _, repo = self.github.members[-1]
            if failure == "missing":
                del self.github.refs[repo, source]
            else:
                self.github.files[repo, self.github.refs[repo, source]]["debian/changelog"] = self.github.changelog("6.1.114")
            with self.subTest(failure=failure), self.assertRaisesRegex(
                    RuntimeError, "源分支不存在" if failure == "missing" else "版本不一致"):
                self.run_propose(False, source)
            self.assertEqual(self.github.writes, [])

    def test_registered_source_resolves_line_id_and_rechecks_reservation_before_writes(self):
        source, line = "develop/releases/old-u1", "stable.u1"
        for member, repo in self.github.members:
            self.github.set_ref(repo, source, self.github.bases[member])
        self.github.cfg["release_lines"][line] = {"member_branch": source}
        self.github.cfg["groups"]["G2"]["versions"][line] = "6.1.113"
        old = self.github.refs[self.github.coordinator, "master"]
        self.github.add_commit(self.github.coordinator,
                               {"groups.yaml": yaml.safe_dump(self.github.cfg, sort_keys=False)},
                               [old], branch="master")
        original, reads = self.github.api, 0
        def api(url, data=None, **kwargs):
            nonlocal reads
            if url == f"repos/{self.github.coordinator}/git/ref/heads/baseline-test-formal-prs-20261113/{line}/G2":
                reads += 1
                if reads == 2:
                    self.baseline(line=line, source_branch=source)
            return original(url, data=data, **kwargs)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "stable.u1.*预留记录"):
            propose.propose(api, self.github.coordinator, self.github.line, self.github.branch,
                            source_branch=source, dry_run=False)
        self.assertEqual(reads, 2)
        self.assertEqual(self.github.writes, [])

    def test_legacy_draft_and_human_historical_sha_survive_moving_source(self):
        first = self.run_propose(False)
        commit = self.github.commits[self.github.coordinator, first["head"]["sha"]]
        saved = json.loads(commit["message"].split(propose.MARKER, 1)[1].split("\n-->", 1)[0])
        saved["source_line"] = saved.pop("source_branch")
        commit["message"] = propose.MARKER + json.dumps(saved) + "\n-->"
        member, repo = self.github.members[0]
        old_files = copy.deepcopy(self.github.files[repo, self.github.bases[member]])
        historical = self.github.add_commit(repo, old_files)
        branch = first["head"]["ref"]
        old = self.github.refs[self.github.coordinator, branch]
        cfg = yaml.safe_load(self.github.files[self.github.coordinator, old]["groups.yaml"])
        cfg["release_lines"][self.github.line]["bases"][member] = historical
        edited = self.github.add_commit(self.github.coordinator, {"groups.yaml": yaml.safe_dump(cfg, sort_keys=False)},
                                        [old], branch=branch, message="Review historical SHA")
        old_files["debian/changelog"] = self.github.changelog("1:6.1.114")
        self.github.add_commit(repo, old_files, [self.github.refs[repo, "master"]], branch="master")
        self.github.calls.clear()
        second = self.run_propose(False)
        self.assertEqual(first["number"], second["number"])
        self.assertEqual(len(self.github.prs), 1)
        result = yaml.safe_load(self.github.files[self.github.coordinator,
                                  self.github.refs[self.github.coordinator, branch]]["groups.yaml"])
        self.assertEqual(result["release_lines"][self.github.line]["bases"][member], historical)
        head = self.github.refs[self.github.coordinator, branch]
        self.assertIn(edited, self.github.ancestors(self.github.coordinator, head))
        self.assertFalse(any(path == f"repos/{repo}/git/ref/heads/master" for path, _, _ in self.github.calls))
        self.assertNotIn(historical, self.github.ancestors(repo, self.github.refs[repo, "master"]))
        self.run_propose(False)
        self.assertEqual(self.github.refs[self.github.coordinator, branch], head)

    def test_partial_pr_failure_reuses_fixed_branch_and_snapshot(self):
        # Fail the write, after the initial read, without creating a duplicate PR.
        original = self.github.api
        failed = False
        def api(url, data=None, **kwargs):
            nonlocal failed
            if url == f"repos/{self.github.coordinator}/pulls" and data and not failed:
                failed = True
                raise RuntimeError("temporary PR error")
            return original(url, data=data, **kwargs)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            propose.propose(api, self.github.coordinator, self.github.line, self.github.branch, dry_run=False)
        branch = f"automation/propose-release-line/{self.github.line}"
        fixed = self.github.refs[self.github.coordinator, branch]
        result = self.run_propose(False)
        self.assertTrue(result["draft"])
        self.assertEqual(self.github.refs[self.github.coordinator, branch], fixed)
        self.assertEqual(len(self.github.prs), 1)

    def test_existing_merged_line_is_never_overwritten(self):
        self.github.with_line()
        self.assertIsNone(self.run_propose(False))
        self.assertEqual(self.github.writes, [])
        with self.assertRaises(RuntimeError):
            propose.propose(self.github.api, self.github.coordinator, self.github.line, "develop/other")

    def test_deleted_ref_with_closed_unmerged_baseline_stays_locked(self):
        self.baseline(deleted=True)
        with self.assertRaisesRegex(RuntimeError, "预留记录"):
            self.run_propose(False)
        self.assertEqual(self.github.writes, [])

    def test_deleted_ref_with_premature_merge_missing_members_stays_locked(self):
        self.baseline(merged=True, deleted=True)
        with self.assertRaisesRegex(RuntimeError, "成员发布尚未完成"):
            self.run_propose(False)
        self.assertEqual(self.github.writes, [])

    def test_member_merge_commit_must_have_exact_recorded_version(self):
        self.baseline(merged=True, deleted=True)
        self.member_merges(wrong_version=True)
        with self.assertRaisesRegex(RuntimeError, "成员发布尚未完成"):
            self.run_propose(False)
        self.assertEqual(self.github.writes, [])

    def test_completed_deleted_ref_is_idle_but_new_reservation_retains_lock(self):
        self.baseline(merged=True, deleted=True)
        self.member_merges()
        self.assertEqual(self.run_propose()["version"], "6.1.113")
        old = self.github.refs[self.github.coordinator, "master"]
        self.github.add_commit(self.github.coordinator, self.github.files[self.github.coordinator, old],
                               [old], branch="baseline-test-formal-prs-20261113/master/G2", message="new reservation")
        with self.assertRaisesRegex(RuntimeError, "预留记录"):
            self.run_propose(False)
        self.assertEqual(self.github.writes, [])

    def test_open_member_release_pr_blocks_unregistered_source_branch(self):
        source = "release/candidate"
        for member, repo in self.github.members:
            self.github.set_ref(repo, source, self.github.bases[member])
        _, repo = self.github.members[0]
        branch = "automation/update-changelog/6.1.114"
        self.github.set_ref(repo, branch, self.github.refs[repo, "master"])
        self.github.add_pr(repo, branch, base=source)
        with self.assertRaisesRegex(RuntimeError, "成员发布 PR"):
            self.run_propose(False, source)
        self.assertEqual(self.github.writes, [])

    def test_old_workflow_environment_cannot_silently_fall_back_to_master(self):
        with patch.dict(os.environ, {"SOURCE_LINE": "develop-2500u3"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "请重新从表单发起"):
                runpy.run_path(str(ROOT / ".github/scripts/propose_release_line.py"), run_name="__main__")

    def test_workflow_is_dispatch_only_and_dry_run_defaults_true(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/propose-release-line.yml").read_text())
        self.assertEqual(set(workflow[True]), {"workflow_dispatch"})
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["source_branch"]["default"], "master")
        self.assertNotIn("source_line", inputs)
        step = workflow["jobs"]["propose"]["steps"][-1]
        self.assertEqual(step["env"]["SOURCE_BRANCH"], "${{ inputs.source_branch }}")
        self.assertIs(inputs["dry_run"]["default"], True)
        self.assertFalse(any("permission-workflows" in step.get("with", {})
                             for step in workflow["jobs"]["propose"]["steps"]))

    def test_yaml_looking_line_ids_remain_strings(self):
        text = yaml.safe_dump(self.github.cfg, sort_keys=False)
        for line in ("on", "yes", "123"):
            with self.subTest(line=line):
                updated = yaml.safe_load(propose.rewrite(text, line, "develop/u1",
                                                         self.github.bases, "6.1.113"))
                self.assertIn(line, updated["release_lines"])
                self.assertIn(line, updated["groups"]["G2"]["versions"])


if __name__ == "__main__":
    unittest.main()
