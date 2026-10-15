#!/usr/bin/env python3
"""Build a static Terraform provider network mirror from GitHub release assets.

Layout under the output directory follows the provider network mirror protocol:

    <hostname>/<namespace>/<type>/index.json
    <hostname>/<namespace>/<type>/<version>.json

Archive URLs point at the GitHub release assets, so the mirror holds only
metadata. Each archive carries a `zh:` hash taken from the release SHA256SUMS
file and an `h1:` hash computed from the archive contents, the same two hash
schemes Terraform writes to .terraform.lock.hcl.
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile

PROVIDER_TYPE = "vkcs"


def gh_json(*args):
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def gh_download(repo, tag, pattern, dest):
    subprocess.run(
        ["gh", "release", "download", tag, "-R", repo, "-p", pattern, "-D", dest, "--clobber"],
        check=True,
    )


def h1_hash(zip_path):
    """Terraform package hash v1: a dirhash Hash1 over the unpacked archive."""
    summary = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist()):
            with zf.open(name) as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            summary.append(f"{digest}  {name}\n")
    total = hashlib.sha256("".join(summary).encode()).digest()
    return "h1:" + base64.b64encode(total).decode()


def zh_hash(sha256_hex):
    return "zh:" + sha256_hex


def parse_platform(asset_name, version):
    prefix = f"terraform-provider-{PROVIDER_TYPE}_{version}_"
    if not asset_name.startswith(prefix) or not asset_name.endswith(".zip"):
        return None
    return asset_name[len(prefix) : -len(".zip")]


def list_release_tags(repo):
    releases = gh_json("release", "list", "-R", repo, "--limit", "200", "--json", "tagName,isDraft,isPrerelease")
    return sorted(
        r["tagName"] for r in releases if not r["isDraft"] and not r["isPrerelease"] and r["tagName"].startswith("v")
    )


def build_version(repo, tag, workdir):
    version = tag[1:]
    assets = gh_json("release", "view", tag, "-R", repo, "--json", "assets")["assets"]
    by_name = {a["name"]: a for a in assets}

    sums_name = f"terraform-provider-{PROVIDER_TYPE}_{version}_SHA256SUMS"
    if sums_name not in by_name:
        print(f"skip {tag}: no {sums_name}", file=sys.stderr)
        return None
    gh_download(repo, tag, sums_name, workdir)
    sums = {}
    with open(os.path.join(workdir, sums_name)) as f:
        for line in f:
            digest, name = line.split()
            sums[name] = digest

    archives = {}
    for name, asset in by_name.items():
        platform = parse_platform(name, version)
        if platform is None:
            continue
        gh_download(repo, tag, name, workdir)
        archives[platform] = {
            "url": asset["url"],
            "hashes": [h1_hash(os.path.join(workdir, name)), zh_hash(sums[name])],
        }
    if not archives:
        print(f"skip {tag}: no provider archives", file=sys.stderr)
        return None
    return version, {"archives": archives}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "purisev/terraform-provider-vkcs"))
    parser.add_argument("--out", required=True, help="output directory (site root)")
    parser.add_argument(
        "--provider",
        action="append",
        default=[],
        metavar="HOST/NAMESPACE/TYPE",
        help="provider address to publish under; may repeat (default: registry.terraform.io/vk-cs/vkcs)",
    )
    args = parser.parse_args()
    providers = args.provider or ["registry.terraform.io/vk-cs/vkcs"]

    versions = {}
    with tempfile.TemporaryDirectory() as workdir:
        for tag in list_release_tags(args.repo):
            built = build_version(args.repo, tag, workdir)
            if built:
                versions[built[0]] = built[1]

    if not versions:
        sys.exit("no releases with provider archives found")

    for provider in providers:
        base = os.path.join(args.out, *provider.split("/"))
        write_json(os.path.join(base, "index.json"), {"versions": {v: {} for v in versions}})
        for version, data in versions.items():
            write_json(os.path.join(base, f"{version}.json"), data)
        print(f"{provider}: {', '.join(sorted(versions))}")


if __name__ == "__main__":
    main()
