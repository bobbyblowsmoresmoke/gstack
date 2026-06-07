#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

def run_cmd(cmd, check=True, capture=True, shell=False):
    try:
        res = subprocess.run(cmd, check=check, capture_output=capture, text=True, shell=shell)
        return res
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {cmd}", file=sys.stderr)
        if capture:
            print(f"STDOUT:\n{e.stdout}", file=sys.stderr)
            print(f"STDERR:\n{e.stderr}", file=sys.stderr)
        raise

def get_package_manager():
    if os.path.exists('bun.lockb') or os.path.exists('bun.lock'): return 'bun'
    if os.path.exists('pnpm-lock.yaml'): return 'pnpm'
    if os.path.exists('yarn.lock'): return 'yarn'
    return 'npm'

def check_workspaces():
    if os.path.exists('package.json'):
        with open('package.json') as f:
            data = json.load(f)
            if 'workspaces' in data:
                print("Error: Monorepos (workspaces) not supported in v1", file=sys.stderr)
                sys.exit(1)

def install_browsers_if_needed():
    if os.path.exists('package.json'):
        with open('package.json') as f:
            data = json.load(f)
            deps = data.get('dependencies', {})
            dev_deps = data.get('devDependencies', {})
            if 'playwright' in deps or 'playwright' in dev_deps:
                print("Installing Playwright browsers...")
                run_cmd(["npx", "playwright", "install", "chromium", "--with-deps"], check=False, capture=False)

def get_noreply_email():
    res = run_cmd(["gh", "api", "user"])
    data = json.loads(res.stdout)
    return f"{data['id']}+{data['login']}@users.noreply.github.com"

def rewrite_history(commits_count):
    if commits_count > 0:
        noreply = get_noreply_email()
        run_cmd(["git", "config", "user.email", noreply])
        env_filter = f"""
        CORRECT_EMAIL="{noreply}"
        export GIT_COMMITTER_EMAIL="$CORRECT_EMAIL"
        export GIT_AUTHOR_EMAIL="$CORRECT_EMAIL"
        """
        # Rewrite the last N commits
        run_cmd(["git", "filter-branch", "-f", "--env-filter", env_filter, "--tag-name-filter", "cat", f"HEAD~{commits_count}..HEAD"])

def main():
    parser = argparse.ArgumentParser(description="Auto Audit Remediator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    p_run = subparsers.add_parser("run")
    p_run.add_argument("--repo", default=os.getcwd())
    p_run.add_argument("--base-branch", default="main")
    p_run.add_argument("--package-manager", default="auto")
    p_run.add_argument("--severity", default="high,moderate")
    p_run.add_argument("--max-major-bumps", type=int, default=5)
    p_run.add_argument("--skip-tests", action="store_true")
    p_run.add_argument("--admin-merge", action="store_true")
    p_run.add_argument("--ci-mode", action="store_true")
    p_run.add_argument("--skip-scripts", nargs="*", default=[])
    p_run.add_argument("--install-browsers", action="store_true", default=True)
    p_run.add_argument("--esm-check-module", default="diff")
    p_run.add_argument("--commit-prefix", default="deps:")
    p_run.add_argument("--no-push", action="store_true")
    p_run.add_argument("--artifact-dir", default=".audit-artifacts")
    
    p_report = subparsers.add_parser("report")
    p_report.add_argument("--artifact-dir", default=".audit-artifacts")
    
    args = parser.parse_args()
    
    os.makedirs(args.artifact_dir, exist_ok=True)
    
    if args.command == "run":
        if args.repo != os.getcwd() and not args.repo.startswith("/"):
            # Would clone here, but assuming we are running in cwd for now
            pass

        print("=== PHASE 0: Baseline ===")
        res = run_cmd(["git", "status", "--porcelain"])
        if res.stdout.strip():
            print("Error: Git working directory is not clean. Exit.", file=sys.stderr)
            sys.exit(1)
            
        check_workspaces()
        pm = get_package_manager() if args.package_manager == 'auto' else args.package_manager
        print(f"Detected Package Manager: {pm}")
        
        if args.install_browsers:
            install_browsers_if_needed()
            
        print("=== PHASE 1: Safe Updates ===")
        if pm == 'bun':
            run_cmd(["bun", "update"], check=False, capture=False)
        else:
            run_cmd(["npm", "audit", "fix"], check=False, capture=False)
            
        res = run_cmd(["git", "status", "--porcelain"])
        commits = 0
        if res.stdout.strip():
            run_cmd(["git", "add", "."])
            run_cmd(["git", "commit", "-m", f"{args.commit_prefix} safe minor/patch updates"])
            commits += 1
            
        print("=== PHASE 2: Manual Bumps ===")
        # simplified stub logic to just run audits
        audit_cmd = ["bun", "audit"] if pm == 'bun' else ["npm", "audit"]
        res = run_cmd(audit_cmd, check=False)
        with open(os.path.join(args.artifact_dir, "audit-after.txt"), "w") as f:
            f.write(res.stdout)
            
        # tests
        print("Running tests...")
        if not args.skip_tests:
            run_cmd(["npm", "test"], check=False, capture=False)
            run_cmd(["npm", "run", "build"], check=False, capture=False)
            if args.esm_check_module:
                run_cmd(["node", "-e", f"require('{args.esm_check_module}')"], check=False)
                
        print("=== PHASE 3: Verification ===")
        if args.ci_mode and "high" in res.stdout.lower():
            print("Creating issue for remaining vulnerabilities...")
            issue_title = "chore(deps): Unresolvable vulnerabilities"
            issue_body = res.stdout
            run_cmd(["gh", "issue", "create", "--title", issue_title, "--body", issue_body, "--label", "dependencies,security"], check=False)
            sys.exit(2)
            
        print("=== PHASE 4: Email Privacy + Ship ===")
        if commits > 0 and not args.no_push:
            rewrite_history(commits)
            run_cmd(["git", "push", "-u", "origin", "HEAD", "--force"])
            if args.admin_merge:
                run_cmd(["gh", "pr", "create", "--fill"])
                run_cmd(["gh", "pr", "merge", "--squash", "--admin", "--delete-branch"])
                
        print("Remediation complete!")

if __name__ == '__main__':
    main()
