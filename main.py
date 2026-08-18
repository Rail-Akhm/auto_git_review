"""Точка входа CLI auto_git_review."""

import argparse

from auto_git_review.alm import AlmClient
from auto_git_review.llm import LlmClient


def cmd_list_prs(args):
    alm = AlmClient()
    data = alm.list_open_pull_requests(args.repo)
    prs = data.get("value", [])
    print(f"Открытых PR: {len(prs)}")
    for pr in prs:
        created_by = pr.get("createdBy", {}).get("displayName", "?")
        print(
            f"  #{pr['pullRequestId']:>5}  {pr['title']}  "
            f"({pr['sourceRefName']} -> {pr['targetRefName']})  by {created_by}"
        )


def cmd_llm_ping(args):
    llm = LlmClient()
    result = llm.chat([{"role": "user", "content": args.message}])
    if result["reasoning"]:
        print("--- reasoning ---")
        print(result["reasoning"])
    print("--- content ---")
    print(result["content"])


def build_parser():
    parser = argparse.ArgumentParser(
        prog="auto_git_review", description="Авто-ревьюер PR в Azure DevOps Server"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_prs = sub.add_parser("list-prs", help="Список открытых PR")
    p_prs.add_argument("--repo", default=None, help="Репозиторий (по умолчанию из .env)")
    p_prs.set_defaults(func=cmd_list_prs)

    p_ping = sub.add_parser("llm-ping", help="Проверка связи с LLM")
    p_ping.add_argument("--message", default="How are you?", help="Сообщение модели")
    p_ping.set_defaults(func=cmd_llm_ping)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
