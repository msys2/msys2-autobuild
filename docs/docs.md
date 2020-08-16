https://mermaid-js.github.io

```
sequenceDiagram
    participant GIT as MSYS2/MINGW-packages
    participant APPVEYOR as Appveyor
    participant API as packages.msys2.org
    participant GHA as GitHub Actions
    participant DT as msys2-autobuild
    participant DEV as Developer
    participant REPO as Pacman Repo

    GIT->>API: webhook trigger on push
    API->>APPVEYOR: trigger PKGBUILD parse
    APPVEYOR->>GIT: fetch PKGBUILDS
    GIT-->>APPVEYOR: 
    APPVEYOR->>APPVEYOR: parse PKGBUILDS
    APPVEYOR-->>API: upload parsed PKGBUILDS

    DT->>GHA: cron trigger
    GHA->>API: fetch TODO list
    API-->>GHA: 
    GHA->>GIT: fetch PKGBUILDs
    GIT-->>GHA: 
    GHA->>DT: fetch staging
    DT-->>GHA: 
    GHA->>GHA: build packages
    GHA-->>DT: upload packages

    DEV->>DT: fetch packages
    DT-->>DEV: 
    DEV->>DEV: sign packages
    DEV->>REPO: push to repo
```

```
{
  "theme": "forest"
}
```