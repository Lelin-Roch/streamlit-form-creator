import streamlit as st
import json
import os
import re
import tempfile
import traceback
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from io import BytesIO
from docx import Document

# --- Constants ---
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/forms.responses.readonly"
]
REDIRECT_URI = st.secrets["google"]["redirect_uri"]
GOOGLE_CLIENT_ID = st.secrets["google"]["client_id"]
GOOGLE_CLIENT_SECRET = st.secrets["google"]["client_secret"]
CREDENTIALS_CACHE_FILE = "cached_credentials.json"

# --- OAuth Helpers ---
def create_oauth_flow():
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uris": [REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token"
        }
    }
    with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.json') as tmp:
        json.dump(client_config, tmp)
        tmp.flush()
        return Flow.from_client_secrets_file(
            tmp.name,
            scopes=SCOPES,
            redirect_uri=REDIRECT_URI,
        )

def save_credentials_to_file(creds):
    with open(CREDENTIALS_CACHE_FILE, "w") as f:
        f.write(creds.to_json())

def load_credentials_from_file():
    try:
        with open(CREDENTIALS_CACHE_FILE, "r") as f:
            creds_data = json.load(f)
            return Credentials.from_authorized_user_info(creds_data)
    except:
        return None

def clear_credentials_file():
    if os.path.exists(CREDENTIALS_CACHE_FILE):
        os.remove(CREDENTIALS_CACHE_FILE)

# --- Combined Question Parser ---
def parse_questions(text):
    questions = []
    blocks = re.split(r'\n(?=\d+\.\s)', text.strip())  # Split by numbered questions

    for block in blocks:
        lines = block.strip().split('\n')
        if not lines:
            continue

        question_line = lines[0].strip()
        question_text = re.sub(r"^\d+\.\s*", "", question_line)

        code_lines = []
        options = []
        correct_answers = []
        qtype = None
        points = 0
        has_lettered_option = False
        has_any_options = False
        i = 1

        # Collect code/description lines
        while i < len(lines):
            line = lines[i].strip()
            if re.match(r"^([*]?\s*✅?\s*)?[A-D]\)", line):  # A) option format
                has_lettered_option = True
                has_any_options = True
                break
            if re.match(r"^\*+\s*", line):  # * option format
                has_any_options = True
                break
            if re.match(r"^✅", line):  # ✅ Option format (no A-D)
                has_any_options = True
                break
            if "CORRECT ANSWER" in line.upper():
                break
            if "TYPE:" in line.upper() or "POINTS:" in line.upper():
                break
            code_lines.append(lines[i])  # Preserve original spacing
            i += 1

        code_block = "\n".join(code_lines)
        is_code_like = any(x in code_block for x in ["=", ":", "def", "{", "}", "return", "print", "import"])
        has_code = bool(code_block.strip()) and is_code_like
        description = code_block if has_code else ""

        # Collect options
        while i < len(lines):
            raw_line = lines[i].strip()
            if not raw_line:
                i += 1
                continue

            if "CORRECT ANSWER" in raw_line.upper():
                letters = raw_line.split(":")[-1].strip().split(",")
                for letter in letters:
                    for opt in options:
                        if opt.startswith(f"{letter.strip().upper()})"):
                            correct_answers.append(opt)
                i += 1
                continue

            if "TYPE:" in raw_line.upper():
                qtype = raw_line.split(":")[-1].strip().upper()
                i += 1
                continue

            if "POINTS:" in raw_line.upper():
                try:
                    points = int(raw_line.split(":")[-1].strip())
                except:
                    points = 0
                i += 1
                continue

            line = raw_line.lstrip("*").strip()
            is_correct = False

            if line.startswith("✅"):
                is_correct = True
                line = line.replace("✅", "", 1).strip()

            match = re.match(r"^([A-D])\)\s*(.*)", line)
            if match:
                letter = match.group(1)
                text = match.group(2).strip()
                full_option = f"{letter}) {text}"
                options.append(full_option)
                if is_correct:
                    correct_answers.append(full_option)
                has_lettered_option = True
                has_any_options = True
            elif line:
                options.append(line)
                if is_correct:
                    correct_answers.append(line)
                has_any_options = True

            i += 1

        # Infer type
        if not qtype:
            if options:
                qtype = "CHECKBOX" if len(correct_answers) > 1 else "MCQ"
            else:
                qtype = "SHORT"

        # Fallback to SHORT if it looks like options but none are marked properly
        if not has_code and options and not correct_answers and not has_lettered_option:
            return_type = "SHORT"
            return_description = "\n".join([f"- {opt}" for opt in options])
            return_options = []
        else:
            return_type = qtype
            return_description = description
            return_options = options

        question_data = {
            "question": question_text,
            "type": return_type,
            "options": return_options,
            "correct_answers": correct_answers,
            "points": points,
            "description": return_description.strip()
        }

        questions.append(question_data)

    return questions



# --- Form Generator ---
def create_google_form(creds, parsed_questions, shuffle=False, form_id=None, quiz_mode=False):
    service = build("forms", "v1", credentials=creds)

    if not form_id:
        form = {
            "info": {
                "title": "Auto-Generated Quiz",
                "documentTitle": "Form from Streamlit App"
            }
        }
        created_form = service.forms().create(body=form).execute()
        form_id = created_form["formId"]

    requests = []

    if quiz_mode:
        requests.append({
            "updateSettings": {
                "settings": {
                    "quizSettings": {
                        "isQuiz": True
                    }
                },
                "updateMask": "quizSettings.isQuiz"
            }
        })

    for q in parsed_questions[::-1]:
        # Split title and code block
        if "\n" in q["question"]:
            parts = q["question"].split("\n", 1)
            title = parts[0].strip()
            description = parts[1]  # Preserve formatting
        else:
            title = q["question"].strip()
            description = q.get("description", "")  # fallback if needed

        # Only sanitize title
        title = title.replace("\n", " ")

        item = {
            "title": title,
            "description": description,
            "questionItem": {
                "question": {
                    "required": True
                }
            }
        }

        if q["type"] in ["MCQ", "CHECKBOX", "DROPDOWN"]:
            qtype_map = {
                "MCQ": "RADIO",
                "CHECKBOX": "CHECKBOX",
                "DROPDOWN": "DROP_DOWN"
            }
            item["questionItem"]["question"]["choiceQuestion"] = {
                "type": qtype_map[q["type"]],
                "options": [{"value": opt} for opt in q["options"]],
                "shuffle": shuffle
            }
        elif q["type"] == "SHORT":
            item["questionItem"]["question"]["textQuestion"] = {"paragraph": False}
        elif q["type"] == "LONG":
            item["questionItem"]["question"]["textQuestion"] = {"paragraph": True}
        elif q["type"] == "DATE":
            item["questionItem"]["question"]["dateQuestion"] = {}
        elif q["type"] == "TIME":
            item["questionItem"]["question"]["timeQuestion"] = {}

        # Add question creation request
        requests.append({
            "createItem": {
                "item": item,
                "location": {"index": 0}
            }
        })

        # Add grading request if needed
        if quiz_mode and q["correct_answers"] and q["type"] in ["MCQ", "CHECKBOX"]:
            correct_answers = q["correct_answers"]
            points = q["points"] if q["points"] else 1

            requests.append({
                "updateItem": {
                    "location": {"index": 0},
                    "item": {
                        "questionItem": {
                            "question": {
                                "grading": {
                                    "correctAnswers": {
                                        "answers": [{"value": ans} for ans in correct_answers]
                                    },
                                    "pointValue": points
                                }
                            }
                        }
                    },
                    "updateMask": "questionItem.question.grading"
                }
            })

    service.forms().batchUpdate(formId=form_id, body={"requests": requests}).execute()
    return form_id

# --- Streamlit UI ---
def main():
    st.set_page_config(page_title="Google Form Creator", layout="centered")

    # --- CSS ---
    st.markdown("""
    <style>
        .block-container {
            padding-top: 1rem;
            padding-bottom: 2rem;
            max-width: 800px;
        }
        .form-box {
            background-color: rgba(255, 255, 255, 0.05);
            padding: 1rem 1.5rem;
            border-radius: 10px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #f1f1f1;
            font-size: 16px;
            margin: 1.5rem auto;
            max-width: 600px;
            text-align: center;
        }
        .btn {
            margin-top: 20px;
            text-align: center;
        }
        .login-btn {
            background-color: white;
            color: black !important;
            padding: 0.6em 1.5em;
            font-weight: bold;
            font-size: 16px;
            border-radius: 8px;
            text-decoration: none !important;
            display: inline-block;
            text-align: center;
            transition: background 0.3s ease, color 0.3s ease;
            border: none;
        }
        .login-btn:hover {
            background-color: #3367D6;
            color: white !important;
        }
        .left-align-preview {
            text-align: left;
        }
        .text {
            text-align: center;
            font-size: 18px;
        }
    </style>
    """, unsafe_allow_html=True)


    st.markdown("<h1 style='text-align: center;'>Google Form Creator</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #888;'>Create Google Forms automatically from .txt, .docx or manual input</p>", unsafe_allow_html=True)

    if "credentials" not in st.session_state:
        creds = load_credentials_from_file()
        st.session_state.credentials = json.loads(creds.to_json()) if creds else None

    query_params = st.query_params
    if "code" in query_params and "state" in query_params and st.session_state.credentials is None:
        try:
            flow = create_oauth_flow()
            auth_response_url = f"{REDIRECT_URI}?code={query_params['code']}&state={query_params['state']}"
            flow.fetch_token(authorization_response=auth_response_url)
            creds = flow.credentials
            st.session_state.credentials = json.loads(creds.to_json())
            save_credentials_to_file(creds)
            st.query_params.clear()  # ✅ Clears the ?code=...&state=...
            st.rerun()
            
        except Exception as e:
            st.error(f"Login failed: {e}")
            st.text(traceback.format_exc())
            return

    if st.session_state.credentials is None:
        st.markdown("<div style='text-align:center; margin-bottom:20px; font-size:20px;'>🔐 Sign in to Continue</div>", unsafe_allow_html=True)
        flow = create_oauth_flow()
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline", include_granted_scopes="true")
        st.markdown(f"<div style='text-align:center'><a href='{auth_url}' class='login-btn'>Sign in with Google</a></div>", unsafe_allow_html=True)
    else:
        creds = Credentials.from_authorized_user_info(st.session_state.credentials)
        try:
            user_service = build("oauth2", "v2", credentials=creds)
            user_info = user_service.userinfo().get().execute()
            st.markdown(f"<div style='text-align:center;'>✅ Logged in as: {user_info['email']}</div>", unsafe_allow_html=True)
        except:
            st.session_state.credentials = None
            clear_credentials_file()
            st.rerun()
            return

        st.markdown("### 📄 Provide Input")
        input_mode = st.radio("Choose Input Mode", ["Upload .txt/.docx File", "Manual Entry"])
        content = ""

        if input_mode == "Upload .txt/.docx File":
            uploaded_file = st.file_uploader("📤 Upload file", type=["txt", "docx"])
            if uploaded_file:
                if uploaded_file.name.endswith(".txt"):
                    content = uploaded_file.read().decode("utf-8")
                elif uploaded_file.name.endswith(".docx"):
                    doc = Document(uploaded_file)
                    content = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        else:
            content = st.text_area("✍️ Paste or type your questions below", height=300)

        form_id = st.text_input("Form ID (optional):", value="")
        quiz_mode = st.checkbox("🎓 Enable Quiz Mode")

        if content:
            questions = parse_questions(content)

            if questions:
                with st.expander("🔍 Preview Questions + Points"):
                    for i, q in enumerate(questions):
                        st.markdown(f"---\n**Q{i+1}: {q['question']}**")

        # Show description/code block if available
                        if q["description"]:
                            st.code(q["description"], language="python")

        # Show options with correct answers highlighted
                        if q["type"] in ["MCQ", "CHECKBOX", "DROPDOWN"] and q["options"]:
                            for opt in q["options"]:
                                if opt in q["correct_answers"]:
                                    st.markdown(f"- ✅ <span style='color:green'><b>{opt}</b></span>", unsafe_allow_html=True)
                                else:
                                    st.markdown(f"- 🔘 {opt}")
                        elif q["type"] == "SHORT":
                            st.markdown("- ✏️ Short Answer")
                        elif q["type"] == "LONG":
                            st.markdown("- 📃 Paragraph Answer")
                        elif q["type"] == "DATE":
                            st.markdown("- 📅 Date")
                        elif q["type"] == "TIME":
                            st.markdown("- ⏰ Time")

        # Points input (if quiz mode is ON)
                        if quiz_mode:
                            questions[i]["points"] = st.number_input(
                                f"Points for Q{i+1}",
                                min_value=0,
                                max_value=100,
                                value=questions[i].get("points", 1),
                                key=f"points_{i}"
                            )
                        else:
                            questions[i]["points"] = 0# Set 0 if quiz mode is off

                shuffle = st.checkbox("🔀 Shuffle answer options", value=False)

                if st.button("🚀 Create Google Form Now"):
                    with st.spinner("⏳ Creating your Google Form..."):
                        try:
                            form_id = create_google_form(creds, questions, shuffle, form_id or None, quiz_mode)
                            form_url = f"https://docs.google.com/forms/d/{form_id}/edit"
                            st.success("✅ Form created successfully!")
                            st.markdown(f"[📄 Open Google Form]({form_url})", unsafe_allow_html=True)
                        except Exception as e:
                            st.error("❌ Failed to create form.")
                            st.text(traceback.format_exc())
            else:
                st.warning("⚠️ No valid questions found in the input.")

        if st.button("🔓 Logout", key="logout_btn"):
            st.session_state.credentials = None
            clear_credentials_file()
            st.query_params.clear()  # ✅ Clears old ?code=...&state=...
            st.rerun()


if __name__ == "__main__":
    main()

