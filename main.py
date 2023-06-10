import os
import json
import requests
from flask import Flask, request
import openai
import tiktoken
import logging
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
logger = logging.getLogger(__name__)

tiktoken_enc = tiktoken.encoding_for_model("gpt-3.5-turbo")
MAX_TOKEN = 1024 * 3

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
    if request.headers.get("X-Gitlab-Event") == "Push Hook":
        handle_push_hook(payload, headers)
    else:
        logger.error("Not supported event: " + request.headers.get("X-Gitlab-Event"))
        return "Not supported event: " + request.headers.get("X-Gitlab-Event"), STATUS_CODE["BAD_REQUEST"]

    return "OK", STATUS_CODE["OK"]

def handle_push_hook(payload, headers):
    project_id = payload["project_id"]
    for commit in payload["commits"]:
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
        else:
            logger.info(msg)
            continue  # Error or No Content

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
        if check_file_type(file_name) or file_name in "-lock.":
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

    pre_prompt = "As a senior developer, review the following code changes and answer code review questions about them. The code changes are provided as git diff strings:"
    questions = "\n\nQuestions:\n1. Can you summarise the changes in a succinct bullet point list, as a separate section in a Changelog like manner\n2. In the diff, are the added or changed code written in a clear and easy to understand way?\n3. Does the code use comments, or descriptive function and variables names that explain what they mean?\n4. based on the code complexity of the changes, could the code be simplified without breaking its functionality? if so can you give example snippets?\n5. Can you find any bugs, if so please explain and reference line numbers?\n6. Do you see any code that could induce security issues?\n"

    messages = [
            {"role": "system", "content": "You are a senior developer reviewing code changes."},
            {"role": "user", "content": f"{pre_prompt}\n\n{''.join(diffs)}{questions}"},
            {"role": "assistant", "content": "Format the response so it renders nicely in GitLab, with nice and organized markdown (use code blocks if needed), and send just the response no comments on the request, when answering include a short version of the question, so we know what it is. In addtion Japanese translations must be added at the end."},
        ]
    logger.info(messages)
    try:
        completions = openai.ChatCompletion.create(
                deployment_id=os.environ.get("OPENAI_API_MODEL"),
                model=os.environ.get("OPENAI_API_MODEL") or "gpt-3.5-turbo",
                temperature=0.7,
                stream=False,
                messages=messages
            )
        answer = completions.choices[0].message["content"].strip()
        answer += "\n\nFor reference, i was given the following questions: \n"
        for question in questions.split("\n"):
            answer += f"\n{question}"
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
