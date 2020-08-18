https://mermaid-js.github.io

```
sequenceDiagram
    participant GIT as MSYS2/MINGW-packages
    participant API as packages.msys2.org
    participant GHA as GitHub Actions
    participant DT as msys2-autobuild
    participant DEV as Developer
    participant REPO as Pacman Repo

    GIT->>GHA: GIT push trigger
    GHA->>GHA: parse PKBUILDs
    GHA-->>GIT: upload parsed PKGBUILDs

loop Every 5 minutes
    API->>GIT: fetch parsed PKGBUILDs
    GIT-->>API: 
end

loop Every 4 hours
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

```
{
  "theme": "forest"
}
```