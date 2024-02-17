from pathlib import Path
from os import environ
from tarfile import open as tar_open
from typing import Dict, List, Tuple, Any, Union
from hashlib import sha512
from shutil import rmtree
from http.client import HTTPSConnection, HTTPResponse, HTTPException, HTTPConnection
from ssl import create_default_context
from json import loads as loads_json
from urllib.request import urlretrieve


def get_ulwgl_proton(env: Dict[str, str]) -> Union[Dict[str, str], None]:
    """Attempt to find existing Proton from the system or downloads the latest if PROTONPATH is not set.

    Only fetches the latest if not first found in .local/share/Steam/compatibilitytools.d
    The cache directory, .cache/ULWGL, is referenced next for latest or as fallback
    """
    files: List[Tuple[str, str]] = []

    try:
        files = _fetch_releases()
    except HTTPException:
        print("Offline.\nContinuing ...")

    cache: Path = Path.home().joinpath(".cache/ULWGL")
    steam_compat: Path = Path.home().joinpath(".local/share/Steam/compatibilitytools.d")

    cache.mkdir(exist_ok=True, parents=True)
    steam_compat.mkdir(exist_ok=True, parents=True)

    # Prioritize the Steam compat
    if _get_from_steamcompat(env, steam_compat, cache, files):
        return env

    # Use the latest Proton in the cache if it exists
    if _get_from_cache(env, steam_compat, cache, files, True):
        return env

    # Download the latest if Proton is not in Steam compat
    # If the digests mismatched, refer to the cache in the next block
    if _get_latest(env, steam_compat, cache, files):
        return env

    # Cache
    # Refer to an old version previously installed
    # Reached on digest mismatch, user interrupt or download failure/no internet
    if _get_from_cache(env, steam_compat, cache, files, False):
        return env

    # No internet and cache/compat tool is empty, just return and raise an exception from the caller
    return env


def _fetch_releases() -> List[Tuple[str, str]]:
    """Fetch the latest releases from the Github API."""
    files: List[Tuple[str, str]] = []
    resp: HTTPResponse = None
    conn: HTTPConnection = HTTPSConnection(
        "api.github.com", timeout=30, context=create_default_context()
    )

    conn.request(
        "GET",
        "/repos/GloriousEggroll/proton-ge-custom/releases",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "",
        },
    )

    resp = conn.getresponse()

    if resp and resp.status != 200:
        return files

    # Attempt to acquire the tarball and checksum from the JSON data
    releases: List[Dict[str, Any]] = loads_json(resp.read().decode("utf-8"))
    for release in releases:
        if "assets" in release:
            assets: List[Dict[str, Any]] = release["assets"]

            for asset in assets:
                if (
                    "name" in asset
                    and (
                        asset["name"].endswith("sum")
                        or (
                            asset["name"].endswith("tar.gz")
                            and asset["name"].startswith("GE-Proton")
                        )
                    )
                    and "browser_download_url" in asset
                ):
                    if asset["name"].endswith("sum"):
                        files.append((asset["name"], asset["browser_download_url"]))
                    else:
                        files.append((asset["name"], asset["browser_download_url"]))

                if len(files) == 2:
                    break
        break

    return files


def _fetch_proton(
    env: Dict[str, str], steam_compat: Path, cache: Path, files: List[Tuple[str, str]]
) -> Dict[str, str]:
    """Download the latest ULWGL-Proton and set it as PROTONPATH."""
    hash, hash_url = files[0]
    proton, proton_url = files[1]

    # TODO: Parallelize this
    print(f"Downloading {hash} ...")
    urlretrieve(hash_url, cache.joinpath(hash).as_posix())
    print(f"Downloading {proton} ...")
    urlretrieve(proton_url, cache.joinpath(proton).as_posix())

    print("Completed.")

    with cache.joinpath(proton).open(mode="rb") as file:
        if (
            sha512(file.read()).hexdigest()
            != cache.joinpath(hash).read_text().split(" ")[0]
        ):
            err: str = "Digests mismatched.\nFalling back to the cache ..."
            raise ValueError(err)
        print(f"{proton}: SHA512 is OK")

    _extract_dir(cache.joinpath(proton), steam_compat)
    environ["PROTONPATH"] = steam_compat.joinpath(proton[: proton.find(".")]).as_posix()
    env["PROTONPATH"] = environ["PROTONPATH"]

    return env


def _extract_dir(proton: Path, steam_compat: Path) -> None:
    """Extract from the cache and to another location."""
    with tar_open(proton.as_posix(), "r:gz") as tar:
        print(f"Extracting {proton} -> {steam_compat.as_posix()} ...")
        tar.extractall(path=steam_compat.as_posix())
        print("Completed.")


def _cleanup(tarball, proton, cache, steam_compat) -> None:
    """Remove files that may have been left in an incomplete state to avoid corruption.

    We want to do this when a download for a new release is interrupted
    """
    print("Keyboard Interrupt received.\nCleaning ...")

    if cache.joinpath(tarball).is_file():
        print(f"Purging {tarball} in {cache} ...")
        cache.joinpath(tarball).unlink()
    if steam_compat.joinpath(proton).is_dir():
        print(f"Purging {proton} in {steam_compat} ...")
        rmtree(steam_compat.joinpath(proton).as_posix())


def _get_from_steamcompat(
    env: Dict[str, str], steam_compat: Path, cache: Path, files: List[Tuple[str, str]]
) -> Dict[str, str]:
    """Refer to Steam compat folder for any existing Proton directories."""
    for proton in steam_compat.glob("GE-Proton*"):
        print(f"{proton.name} found in: {steam_compat.as_posix()}")
        environ["PROTONPATH"] = proton.as_posix()
        env["PROTONPATH"] = environ["PROTONPATH"]

        # Notify the user that they're not using the latest
        if len(files) == 2 and proton.name != files[1][0][: files[1][0].find(".")]:
            print(
                "GE-Proton is outdated and requires manual intervention.\nFor latest release, please download "
                + files[1][1]
            )

        return env

    return None


def _get_from_cache(
    env: Dict[str, str],
    steam_compat: Path,
    cache: Path,
    files: List[Tuple[str, str]],
    use_latest=True,
) -> Dict[str, str]:
    """Refer to ULWGL cache directory.

    Use the latest in the cache when present. Older Proton versions are only referred to when: digests mismatch, user interrupt, or download failure/no internet
    """
    path: Path = None
    name: str = ""

    for tarball in cache.glob("GE-Proton*.tar.gz"):
        if files and tarball == cache.joinpath(files[1][0]) and use_latest:
            path = tarball
            name = tarball.name
        elif not use_latest:
            path = tarball
            name = tarball.name
        print(f"{tarball.name} found in: {cache.as_posix()}")
        break

    if path:
        try:
            _extract_dir(path, steam_compat)
            environ["PROTONPATH"] = steam_compat.joinpath(
                name[: name.find(".")]
            ).as_posix()
            env["PROTONPATH"] = environ["PROTONPATH"]

            return env
        except KeyboardInterrupt:
            proton: str = name[: name.find(".")]

            if steam_compat.joinpath(proton).is_dir():
                print(f"Purging {proton} in {steam_compat} ...")
                rmtree(steam_compat.joinpath(proton).as_posix())
            raise

    return None


def _get_latest(
    env: Dict[str, str], steam_compat: Path, cache: Path, files: List[Tuple[str, str]]
) -> Dict[str, str]:
    """Download the latest Proton for new installs -- empty cache and Steam compat.

    When the digests mismatched or when interrupted, refer to cache for an old version
    """
    if files:
        print("Fetching latest release ...")
        try:
            _fetch_proton(env, steam_compat, cache, files)
            env["PROTONPATH"] = environ["PROTONPATH"]
        except ValueError:
            # Digest mismatched or download failed
            # Refer to the cache for old version next
            return None
        except KeyboardInterrupt:
            tarball: str = files[1][0]

            # Exit cleanly
            # Clean up extracted data and cache to prevent corruption/errors
            # Refer to the cache for old version next
            _cleanup(tarball, tarball[: tarball.find(".")], cache, steam_compat)
            return None

    return env
