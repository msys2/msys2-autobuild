# msys2-devtools

## autobuild.py

```console
$ python -m pip install --user -r requirements.txt
```

```console
$ python autobuild.py --help
usage: autobuild.py [-h] {build,show,show-assets,fetch-assets,trigger,clean-assets} ...

Build packages

optional arguments:
  -h, --help            show this help message and exit

subcommands:
  {build,show,show-assets,fetch-assets,trigger,clean-assets}
    build               Build all packages
    show                Show all packages to be built
    show-assets         Show all staging packages
    fetch-assets        Download all staging packages
    trigger             Trigger a GHA build
    clean-assets        Clean up GHA assets
```

## Build Process

The following graph shows what happens between a PKGBUILD getting changed in git and the built package being available in the pacman repo.

![sequence](./docs/sequence.svg)

Security considerations:

TODO