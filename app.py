from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
import yaml
import flask_sock
import json
from datetime import datetime

app = Flask(__name__)
ws = flask_sock.Sock(app)

with open('config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

app.config['SECRET_KEY'] = config['secret_key']

with open('quiz.yaml', 'r', encoding="utf-8") as f:
    quiz_data = yaml.safe_load(f)

try:
    with open('answers.yaml', 'r', encoding="utf-8") as f:
        answers_data = yaml.safe_load(f)
except FileNotFoundError:
    print("Answers file not found, initializing empty answers data.")
    answers_data = {}
# Ensure answers_data is a dictionary with question IDs as keys
for question in quiz_data['questions']:
    if question['id'] not in answers_data:
        answers_data[question['id']] = {team: {sqid: {'answer': None, 'rating': 0} for sqid in range(len(question['subquestions']))} for team in config['teams']}

active_question_id = 1
ws_clients = []

def send_to_all_clients(message):
    for client in ws_clients:
        client.send(json.dumps(message))

@app.route('/')
def index():
    return redirect(url_for('contestant'))

@app.route('/select_team', methods=['GET', 'POST'])
def select_team():
    if request.method == 'POST':
        team = request.form['team']
        session['team'] = team
        return redirect(url_for('contestant'))
    return render_template('select_team.html', teams=config['teams'])

@app.route('/contestant')
def contestant():
    if 'team' not in session:
        return redirect(url_for('select_team'))
    return render_template('contestant.html')

def update_answers_file():
    with open('answers.yaml', 'w', encoding='utf-8') as f:
        yaml.safe_dump(answers_data, f, allow_unicode=True)

@app.route('/submit_answer', methods=['POST'])
def submit_answer():
    data = request.form
    team = session.get('team')
    if not team:
        raise ValueError("Team not selected.")
    active_question = next((q for q in quiz_data['questions'] if q['id'] == active_question_id), None)
    if not active_question:
        raise ValueError("No active question found.")
    
    for idx, sq in enumerate(active_question['subquestions']): # TODO: Subquestions should have an ID
        # TODO: The data model for answers is bad
        answer = data.get(f'answer-{idx}')
        if answer is None:
            raise ValueError(f"Answer for subquestion {idx} is missing.")
        
        # Store the answer in answers_data
        if active_question['type'] == 'number':
            try:
                answer = float(answer)
            except ValueError:
                raise ValueError(f"Invalid number format for subquestion {idx}.")
        elif active_question['type'] == 'time':
            # Parse as seconds since midnight
            answer = answer.strip()
            try:
                t = datetime.strptime(answer, "%H:%M:%S")
            except ValueError:
                try:
                    t = datetime.strptime(answer, "%H:%M")
                except ValueError:
                    raise ValueError(f"Invalid time format for subquestion {idx}.")
            answer = t.hour * 3600 + t.minute * 60 + t.second
        answers_data[active_question_id][team][idx]['answer'] = answer

        # Automtic rating
        if active_question['type'] in ['text', 'single_choice']:
            assert type(answer) is str
            if answer.strip().lower() == sq['answer'].strip().lower():
                answers_data[active_question_id][team][idx]['rating'] = 1
            else:
                answers_data[active_question_id][team][idx]['rating'] = 0
        elif active_question['type'] == 'number':
            assert type(answer) is float
            correct_value = sq['answer']
            if abs(answer - correct_value) / correct_value <= config['two_point_cutoff']:
                answers_data[active_question_id][team][idx]['rating'] = 2
            elif abs(answer - correct_value) / correct_value <= config['one_point_cutoff']:
                answers_data[active_question_id][team][idx]['rating'] = 1
            else:
                answers_data[active_question_id][team][idx]['rating'] = 0
        elif active_question['type'] == 'time':
            assert type(answer) is int
            # use seconds
            correct_value = sq['answer']
            if abs(answer - correct_value) / correct_value <= config['two_point_cutoff']:
                answers_data[active_question_id][team][idx]['rating'] = 2
            elif abs(answer - correct_value) / correct_value <= config['one_point_cutoff']:
                answers_data[active_question_id][team][idx]['rating'] = 1
            else:
                answers_data[active_question_id][team][idx]['rating'] = 0

    # Save the updated answers data to the file
    update_answers_file()

    # Notify all websocket clients that answers have changed
    send_to_all_clients({'msg': 'answers_updated', 'question_id': active_question_id})

    flash('Antwort wurde gespeichert.', 'success')

    return redirect(url_for('contestant'))

@app.route('/get_active_question')
def get_active_question():
    question = next((q for q in quiz_data['questions'] if q['id'] == active_question_id), None)
    if question:
        return render_template('question.html', question=question)
    else:
        return ('Question not found', 404)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == config['admin_password']:
            session['admin_logged_in'] = True
            flash('Admin-Bereich freigeschaltet.', 'success')
            return redirect(url_for('admin'))
        else:
            flash('Falsches Passwort.', 'danger')
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    flash('Abgemeldet.', 'success')
    return redirect(url_for('admin_login'))

@app.route('/admin')
def admin():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    return render_template('admin.html',
                           questions=quiz_data['questions'],
                           active_question=next((q for q in quiz_data['questions'] if q['id'] == active_question_id)),
                           teams=config['teams'],
                           answers=answers_data,
                           )

@app.route('/admin/answers_table')
def admin_answers_table():
    if not session.get('admin_logged_in'):
        return ('', 403)
    return render_template('admin_answers_table.html',
                           active_question=next((q for q in quiz_data['questions'] if q['id'] == active_question_id)),
                           teams=config['teams'],
                           answers=answers_data,
                           )

@app.route('/admin/set_active_question', methods=['POST'])
def set_active_question():
    q_id = request.form.get('question_id', type=int)
    if q_id is None or not any(q['id'] == q_id for q in quiz_data['questions']):
        flash('Ungültige Frage-ID.', 'error')
        return redirect(url_for('admin'))
    global active_question_id
    active_question_id = q_id
    send_to_all_clients({'msg': 'active_question_changed', 'question_id': q_id})
    flash(f'Aktive Frage wurde auf {q_id} gesetzt.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/set_rating', methods=['POST'])
def set_rating():
    if not session.get('admin_logged_in'):
        return ('', 403)
    data = request.get_json()
    team = data['team']
    question_id = data['question_id']
    subq_idx = int(data['subq_idx'])
    rating = int(data['rating'])
    answers_data[question_id][team][subq_idx]['rating'] = rating
    # Save to file
    update_answers_file()
    send_to_all_clients({'msg': 'answers_updated', 'question_id': question_id})
    return ('', 204)

@app.route('/admin/scores')
def admin_scores():
    if not session.get('admin_logged_in'):
        return ('', 403)
    # Prepare data for the scores table
    teams = config['teams']
    questions = quiz_data['questions']
    # Build a list of all (question_id, subq_idx) pairs for columns
    subq_columns = []
    for q in questions:
        for subq_idx, sq in enumerate(q['subquestions']):
            subq_columns.append({
                'question_id': q['id'],
                'subq_idx': subq_idx,
                'question_text': q['text'],
                'subquestion_text': sq['question']
            })
    # Build a dict: team -> [ratings per subq], and total
    team_scores = {}
    for team in teams:
        ratings = []
        total = 0
        for col in subq_columns:
            qid = col['question_id']
            sqidx = col['subq_idx']
            rating = 0
            rating = answers_data[qid][team][sqidx]['rating']
            ratings.append(rating)
            total += rating
        team_scores[team] = {'ratings': ratings, 'total': total}
    # Sort teams by total score descending
    sorted_team_scores = sorted(team_scores.items(), key=lambda x: x[1]['total'], reverse=True)
    return render_template('admin_scores.html',
                           teams=[team for team, _ in sorted_team_scores],
                           subq_columns=subq_columns,
                           team_scores=dict(sorted_team_scores))

@ws.route('/ws')
def websocket(ws):
    ws_clients.append(ws)
    try:
        while True:
            message = ws.receive()
            if message is None:
                break
            # Handle incoming messages if needed
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        ws_clients.remove(ws)
