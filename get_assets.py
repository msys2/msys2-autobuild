from os import environ
from github import Github
from tabulate import tabulate


def show_assets(type_):
    gh = Github(environ["GITHUB_TOKEN"])
    #gh = Github(environ["GITHUB_USER"], environ["GITHUB_PASS"])

    assets = gh.get_repo('msys2/msys2-devtools').get_release('staging-' + type_).get_assets()

    print(tabulate(
        [[
            asset.name,
            asset.size,
            asset.created_at,
            asset.updated_at,
            #asset.browser_download_url
        ] for asset in assets],
        headers=["name", "size", "created", "updated"] #, "url"]
    ))

show_assets("msys")
show_assets("mingw")