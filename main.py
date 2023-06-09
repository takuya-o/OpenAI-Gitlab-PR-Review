import os
import json
import requests
from flask import Flask, request
import openai

app = Flask(__name__)
openai.api_key = os.environ.get("OPENAI_API_KEY")
if not openai.api_key:
    print( "Error: No OPENAI_API_KEY")
gitlab_token = os.environ.get("GITLAB_TOKEN")
gitlab_url = os.environ.get("GITLAB_URL")
expected_gitlab_token = os.environ.get("EXPECTED_GITLAB_TOKEN")

api_base = os.environ.get("AZURE_OPENAI_API_BASE")
if api_base:
    print(f"API_BASE: {api_base}")
    openai.api_base = api_base

openai.api_version = os.environ.get("AZURE_OPENAI_API_VERSION")
if openai.api_version:
    print("API TYPE: azure")
    openai.api_type = "azure"

@app.route('/webhook', methods=['POST'])
def webhook():
    global gitlab_token, gitlab_url
    if expected_gitlab_token and request.headers.get("X-Gitlab-Token") != expected_gitlab_token:
        return "Unauthorized", 403
    if gitlab_token is None:
        gitlab_token = request.headers.get("X-Gitlab-Token")
    if gitlab_url is None:
        gitlab_url = request.headers.get("X-Gitlab-Instance") + "/api/v4"
    payload = request.json
    print(payload)
    headers = {"Private-Token": gitlab_token}
    if request.headers.get("X-Gitlab-Event") == "Merge Request Hook":
        action = payload["object_attributes"]["action"]
        if action != "open":
            print("Not a MR open. " + action)
            return "Not a MR open. " + action, 204 # No Content
        project_id = payload["project"]["id"]
        mr_id = payload["object_attributes"]["iid"]
        changes_url = f"{gitlab_url}/projects/{project_id}/merge_requests/{mr_id}/diffs"  # 20diff/page ?page=1&per_page=20

        response = requests.get(changes_url, headers=headers)
        mr_changes = response.json()
        if isinstance(mr_changes, dict) and "error" in mr_changes:
            msg = f"Error: {mr_changes.get('error')} URL={changes_url} HEADERS={headers}"
            print (msg, flush=True)
            return msg, 502 # Bad Gateway
        if len(mr_changes) == 0:
            print("No changes", flush=True)
            return "No changes", 204 # No Content

        diffs = [change["diff"] for change in mr_changes]

        answer = chatComplitionDiffs(diffs)
        comment_url = f"{gitlab_url}/projects/{project_id}/merge_requests/{mr_id}/notes"
        comment_payload = {"body": answer}
        comment_response = requests.post(comment_url, headers=headers, json=comment_payload)
    elif request.headers.get("X-Gitlab-Event") == "Push Hook":
        project_id = payload["project_id"]

        for commit in payload["commits"]:
            commit_id = commit["id"]
            commit_url = f"{gitlab_url}/projects/{project_id}/repository/commits/{commit_id}/diff"

            response = requests.get(commit_url, headers=headers)
            changes = response.json()
            print(changes)
            if isinstance(changes, dict):
                if "error" in changes:
                    print(f"Error: {changes.get('error')} URL={commit_url} HEADERS={headers}", flush=True)
                elif "message" in changes:
                    if changes.get("message") == "403 Forbidden":
                        print(f"Error: The Reportor role is nessary to access. URL={commit_url}", flush=True)
                    else:
                        print(f"Error: {changes.get('message')} URL={commit_url}", flush=True)
                else:
                    print(f"Error: {changes} URL={commit_url}", flush=True)
                continue
            if not isinstance(changes, list):
                print(f"Error: {changes} URL={commit_url}", flush=True)
                continue
            if len(changes) == 0:
                print(f"No changes URL={commit_url}", flush=True)
                continue

            diffs = [change.get("diff") for change in changes]

            answer = chatComplitionDiffs(diffs)
            comment_url = f"{gitlab_url}/projects/{project_id}/repository/commits/{commit_id}/comments"
            comment_payload = {"note": answer}
            comment_response = requests.post(comment_url, headers=headers, json=comment_payload)

    return "OK", 200

def chatComplitionDiffs(diffs):
    pre_prompt = "As a senior developer, review the following code changes and answer code review questions about them. The code changes are provided as git diff strings:"
    questions = "\n\nQuestions:\n1. Can you summarise the changes in a succinct bullet point list, as a separate section in a Changelog like manner\n2. In the diff, are the added or changed code written in a clear and easy to understand way?\n3. Does the code use comments, or descriptive function and variables names that explain what they mean?\n4. based on the code complexity of the changes, could the code be simplified without breaking its functionality? if so can you give example snippets?\n5. Can you find any bugs, if so please explain and reference line numbers?\n6. Do you see any code that could induce security issues?\n"

    messages = [
            {"role": "system", "content": "You are a senior developer reviewing code changes."},
            {"role": "user", "content": f"{pre_prompt}\n\n{''.join(diffs)}{questions}"},
            {"role": "assistant", "content": "Format the response so it renders nicely in GitLab, with nice and organized markdown (use code blocks if needed), and send just the response no comments on the request, when answering include a short version of the question, so we know what it is. In addtion Japanese translations must be added at the end."},
        ]
    print(messages)
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
    except Exception as e:
        print(e)
        answer = "I'm sorry, I'm not feeling well today. Please ask a human to review this code change."
        answer += "\n\nThis comment was generated by an artificial intelligence duck."
        answer += "\n\nError: " + str(e)

    print(answer, flush=True)
    return answer


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
