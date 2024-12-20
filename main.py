import os
import json
from threading import Thread
import requests
from flask import Flask, request
import openai
import tiktoken
import logging

DEFAULT_MODEL = "gpt-4o-mini"

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
logger = logging.getLogger(__name__)

tiktoken_enc = tiktoken.encoding_for_model(os.environ.get("OPENAI_API_MODEL") or DEFAULT_MODEL)
MAX_TOKEN = int(os.environ.get("OPENAI_API_TOKEN_LIMIT") or (1024 * 3))

STATUS_CODE = {
    "BAD_GATEWAY": 502,
    "BAD_REQUEST": 400,
    "UNAUTHORIZED": 403,
    "NO_CONTENT": 204,
    "OK": 200
}
app = Flask(__name__)
openai.api_key = os.environ.get("OPENAI_API_KEY")
if not openai.api_key:
    logger.error( "Error: No OPENAI_API_KEY")
gitlab_token = os.environ.get("GITLAB_TOKEN")
gitlab_url = os.environ.get("GITLAB_URL")
expected_gitlab_token = os.environ.get("EXPECTED_GITLAB_TOKEN")
if gitlab_token and gitlab_token != expected_gitlab_token and not gitlab_url:
    logger.warning( "Warning:  It is strongly recommended to set the GITLAB_URL because the GITLAB_TOKEN is set.")

api_base = os.environ.get("AZURE_OPENAI_API_BASE")
if api_base:
    logger.info(f"API_BASE: {api_base}")
    openai.api_base = api_base

openai.api_version = os.environ.get("AZURE_OPENAI_API_VERSION")
if openai.api_version:
    logger.info(f"API TYPE: azure {openai.api_version}")
    openai.api_type = "azure"

@app.route('/webhook', methods=['POST'])
def webhook():
    global gitlab_token, gitlab_url
    if expected_gitlab_token and request.headers.get("X-Gitlab-Token") != expected_gitlab_token:
        return "Unauthorized", STATUS_CODE["UNAUTHORIZED"]
    if gitlab_token is None:
        gitlab_token = request.headers.get("X-Gitlab-Token")
    if gitlab_url is None:
        gitlab_url = request.headers.get("X-Gitlab-Instance") + "/api/v4"
    payload = request.json
    logger.info(payload)
    headers = {"Private-Token": gitlab_token}
    event_handlers = {
        "Push Hook": handle_push_hook,
        "Note Hook": handle_note_hook  # assuming handle_note_hook is a function similar to handle_push_hook
    }
    event_type = request.headers.get("X-Gitlab-Event")
    handler = event_handlers.get(event_type)
    if handler is not None:
        t = Thread(target=handler, args=(payload, headers))
        t.start()
    else:
        logger.error("Not supported event: " + event_type)
        return "Not supported event: " + event_type, STATUS_CODE["BAD_REQUEST"]
    return "OK", STATUS_CODE["OK"]

def handle_note_hook(payload, headers):
    logger.info("Comment Hook")
    if payload["object_attributes"]["noteable_type"] != "Commit":
        # Commitのコメントではない場合には何もしない
        logger.info("Not comment for commit")
        return
    else:
        note = payload["object_attributes"]["note"]
        if not note.startswith("@chatgpt "):
            # noteが"@chatgpt "で始まっていない場合は何もしない
            logger.info("Not for @chatgpt comment")
            return
    # commitに対するコメントで"@chatgpt "で始まっている
    logger.info("Commit Comment for @chatgpt")
    # https://docs.gitlab.com/ee/user/project/integrations/webhook_events.html#comment-events
    project_id = payload["project_id"]
    commit_id = payload["commit"]["id"]
    commit_url = f"{gitlab_url}/projects/{project_id}/repository/commits/{commit_id}/diff"
    response = requests.get(commit_url, headers=headers)
    changes = response.json()
    msg = check_changes(changes, commit_url)
    if msg == "":
        answer = chatComplitionDiffs(changes) # 何を聞いてもDiffのコードレビューだけ
        if payload["object_attributes"]["type"] == "DiscussionNote":
            # "type": "DiscussionNote" の場合はディスカッションに追加する
            # https://docs.gitlab.com/ee/api/discussions.html#add-note-to-existing-commit-thread
            discussion_id = payload['object_attributes']['discussion_id']
            comment_url = f"{gitlab_url}/projects/{project_id}/repository/commits/{commit_id}/discussions/{discussion_id}/notes"
            comment_payload = {"body": answer }
        else:
            # 普通のcommitに対するコメントなので、普通にコメントする
            # https://docs.gitlab.com/ee/api/commits.html#post-comment-to-commit
            comment_url = f"{gitlab_url}/projects/{project_id}/repository/commits/{commit_id}/comments"
            comment_payload = {"note": answer}
        comment_response = requests.post(comment_url, headers=headers, json=comment_payload)
        log_gitlab_response(comment_response) # エラーでも気にしない
    else:
        logger.info(msg)

def handle_push_hook(payload, headers):
    logger.info("Push Hook")
    project_id = payload["project_id"]
    commits = payload["commits"] or []
    if len(commits) == 0:
        # failsafe Commitのコメントではない場合には何もしない
        logger.info("Not comment for commit")
        return
    for commit in commits:
        commit_id = commit["id"]
        commit_url = f"{gitlab_url}/projects/{project_id}/repository/commits/{commit_id}/diff"
        title = commit["title"]
        if title.startswith("Merge branch "):
            logger.info(f"SKIP: Merge branch {title} URL={commit_url}")
            continue  # Ignore merge branch
        if title.startswith("Merge pull request "): # for GitHub?
            logger.info(f"SKIP Merge pull request {title} URL={commit_url}")
            continue  # Ignore merge pull request
        response = requests.get(commit_url, headers=headers)
        changes = response.json()
        msg = check_changes(changes, commit_url)
        if msg == "":
            answer = chatComplitionDiffs(changes)
            comment_url = f"{gitlab_url}/projects/{project_id}/repository/commits/{commit_id}/comments"
            comment_payload = {"note": answer}
            comment_response = requests.post(comment_url, headers=headers, json=comment_payload)
            log_gitlab_response(comment_response) # エラーでも気にしない
        else:
            logger.info(msg)
            continue  # Error or No Content

def log_gitlab_response(comment_response):
    logger.info("GitLab comment POST Response: " + comment_response.text)

def check_changes(changes, commit_url):
    logger.debug(changes)
    if isinstance(changes, dict):
        if "error" in changes:
            msg = f"Error: {changes.get('error')} URL={commit_url}"
        elif "message" in changes:
            if changes.get("message") == "403 Forbidden":
                msg = f"Error: The Reportor role is nessary to access. URL={commit_url}"
            else:
                msg = f"Error: {changes.get('message')} URL={commit_url}"
        else:
            msg = f"Error: {changes} URL={commit_url}"
        return msg
    if not isinstance(changes, list):
        msg = f"Error: {changes} URL={commit_url}"
        return msg
    if len(changes) == 0:
        msg = f"No changes URL={commit_url}"
        return msg
    return ""

extensions = set([
    ".lock",
    ".svg", ".png", ".jpg", ".jpeg", ".gif",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".gz", ".bz2", ".tar", ".iso",
    ".apk", ".exe", ".dmg", ".deb", ".rpm", ".cab", ".torrent",
    ".mp3", ".wav", ".ogg", ".flac", ".aac",
    ".mp4", ".mov", ".avi", ".wmv", ".webm", ".mkv", ".flv", ".m4v", ".mpeg", ".mpg", ".m2v", ".m4a", ".m4b", ".m4p", ".m4r", ".m4v"
    ])

def check_file_type(file_name):
    # ファイル拡張子をセットに変換してO(1)時間でチェック
    return file_name.endswith(tuple(extensions))

def count_token(src):
    tokens = tiktoken_enc.encode(src)
    return len(tokens)

def chatComplitionDiffs(changes):
    diffs = []
    sum_token = 0
    for change in changes:
        file_name = change.get("new_path")
        if check_file_type(file_name) or "-lock." in file_name:
            logger.info(f"SKIP: {file_name}")
            continue
        diff = change.get("diff")
        count = count_token(diff)
        logger.debug(f"Count token:{count} {file_name}")
        if ( sum_token + count ) > MAX_TOKEN:
            logger.warning(f"TOO LONG TOKEN: {sum_token} + {count} > {MAX_TOKEN} {file_name}")
            continue
        diffs.append(diff)
        sum_token += count

    #pre_prompt = "Review the following git diff code changes, focusing on structure, security, and clarity."
    pre_prompt = "Review the git diff of a recent commit, focusing on clarity, structure, and security."

    # questions = """
    # Questions:
    # 1. Summarize key changes.
    # 2. Is the new/modified code clear?
    # 3. Are comments and names descriptive?
    # 4. Can complexity be reduced? Examples?
    # 5. Any bugs? Where?
    # 6. Potential security issues?
    # 7. Suggestions for best practices alignment?
    # """
    questions = """
    Questions:
    1. Summarize changes (Changelog style).
    2. Clarity of added/modified code?
    3. Comments and naming adequacy?
    4. Simplification without breaking functionality? Examples?
    5. Any bugs? Where?
    6. Potential security issues?
    7. Suggestions for best practices alignment?
    """

    messages = [
        {"role": "system", "content": (
            "You are a senior developer reviewing code changes from a commit."
            " You MUST answer by Japanese."
        )},
        {"role": "user", "content": f"{pre_prompt}\n\n{''.join(diffs)}{questions}"},
        {"role": "assistant", "content": (
            "Respond in markdown for GitLab. Include concise versions of questions in the response." 
            " No need to wrap the entire answer in a markdown code block."

        )},
    ]

    logger.info(messages)
    try:
        completions = openai.ChatCompletion.create(
                deployment_id=os.environ.get("OPENAI_API_MODEL"),
                model=os.environ.get("OPENAI_API_MODEL") or DEFAULT_MODEL,
                temperature=0.2,
                stream=False,
                messages=messages
            )
        answer = completions.choices[0].message["content"].strip()
        answer += "\n\nThis comment was generated by an artificial intelligence duck."
        answer += f"\nPrompt:{completions.usage.prompt_tokens} Completion:{completions.usage.completion_tokens} Total:{completions.usage.total_tokens}"
    except Exception as e:
        logger.error(e)
        answer = "I'm sorry, I'm not feeling well today. Please ask a human to review this code change."
        answer += "\n\nThis comment was generated by an artificial intelligence duck."
        answer += "\n\nError: " + str(e)

    logger.info(answer)
    return answer


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
