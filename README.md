# msys2-autobuild

## CLI

```console
$ pacman -S mingw-w64-x86_64-python-tabulate mingw-w64-x86_64-python-pygithub mingw-w64-x86_64-python-requests
# or
$ poetry install
# or
$ python -m pip install --user -r requirements.txt
```

```console
$ msys2-autobuild --help
usage: msys2-autobuild [-h] [-R REPO]
                       {build,show,write-build-plan,update-status,fetch-assets,upload-assets,clear-failed}
                       ...

Build packages

options:
  -h, --help            show this help message and exit
  -R REPO, --repo REPO  msys2-autobuild repository to target (default 'msys2/msys2-autobuild')

subcommands:
  {build,show,write-build-plan,update-status,fetch-assets,upload-assets,clear-failed}
    build               Build all packages
    show                Show all packages to be built
    write-build-plan    Write a GHA build matrix setup
    update-status       Update the status file
    fetch-assets        Download all staging packages
    upload-assets       Upload packages
    clear-failed        Clear the failed state for packages
```

## Automated Build Process

The following graph shows what happens between a PKGBUILD getting changed in git
and the built package being available in the pacman repo.

```mermaid
sequenceDiagram
    participant GIT as MSYS2/MINGW-packages
    participant API as packages.msys2.org
    participant GHA as GitHub Actions
    participant DT as msys2-autobuild
    participant DEV as Developer
    participant REPO as Pacman Repo

    GIT->>GHA: GIT push trigger
    GHA->>GHA: parse PKGBUILDs
    GHA-->>GIT: upload parsed PKGBUILDs

loop Every 5 minutes
    API->>GIT: fetch parsed PKGBUILDs
    GIT-->>API: 
end

loop Every 2 hours
    DT->>GHA: cron trigger
    GHA->>API: fetch TODO list
    API-->>GHA: 
    GHA->>GIT: fetch PKGBUILDs
    GIT-->>GHA: 
    GHA->>DT: fetch staging
    DT-->>GHA: 
    GHA->>GHA: build packages
    GHA-->>DT: upload packages
end

    DEV->>DT: fetch packages
    DT-->>DEV: 
    DEV->>DEV: sign packages
    DEV->>REPO: push to repo
```

### Security Considerations

Assuming changes to PKGBUILDs are properly reviewed, the pacman signature
checking works, the upstream source is OK and all MSYS2 organization members are
trusted we need to consider a bad actor controlling some part of the building
process between the PKGBUILD getting changed and the package ending up signed in
the pacman repo.

A bad actor would need to get a package on the machine of the developer signing
the package and adding it to the pacman repo. We take the following precautions:

* We only build packages automatically with GitHub Actions without third party
  actions, excluding the official GitHub ones. We assume the GHA images and
  official actions are safe.
* The download tool used by the person signing the package checks that the
  binaries where uploaded by a restricted set of GitHub users or GHA.
  We assume the bad actor doesn't have git push rights.
* Packages too large for GHA get built/signed by MSYS2 developers on their
  machines. We assume the developer machines are safe.
* We enforce 2FA for the MSYS2 organization to make account takeovers of
  existing MSYS2 developers harder.

Feedback and ideas on how to improve this welcome.
