# msys2-autobuild

msys2-autobuild is a Python tool for

* automatically building MSYS2 packages in GitHub Actions
* manually uploading packages, or retrying builds
* retrieving the built packages for upload to the pacman repo

## Installation

```console
$ pacman -S mingw-w64-x86_64-python-tabulate mingw-w64-x86_64-python-pygithub mingw-w64-x86_64-python-requests
# or
$ uv sync
# or
$ python -m pip install --user -r requirements.txt
# or
$ uv tool install git+https://github.com/msys2/msys2-autobuild
```

## Usage

```console
$ msys2-autobuild --help
usage: msys2-autobuild [-h]
                       {build,show,write-build-plan,update-status,fetch-assets,upload-assets,clear-failed,clean-assets}
                       ...

Build packages

options:
  -h, --help            show this help message and exit

subcommands:
  {build,show,write-build-plan,update-status,fetch-assets,upload-assets,clear-failed,clean-assets}
    build               Build all packages
    show                Show all packages to be built
    write-build-plan    Write a GHA build matrix setup
    update-status       Update the status file
    fetch-assets        Download all staging packages
    upload-assets       Upload packages
    clear-failed        Clear the failed state for packages
    clean-assets        Clean up GHA assets
```

## Configuration

* `GITHUB_TOKEN` (required) - a GitHub token with write access to the current repo.
* `GITHUB_TOKEN_READONLY` (optional) - a GitHub token with read access to the current repo. This is used for read operations to not get limited by the API access limits.
* `GITHUB_REPOSITORY` (optional) - the path to the GitHub repo this is uploading to. Used for deciding which things can be built and where to upload them to. Defaults to `msys2/msys2-autobuild`.
