import os
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request, flash, jsonify,
    send_from_directory, abort
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from werkzeug.utils import secure_filename
from sqlalchemy import inspect, text

from models import (
    db, User, Space, Ministry, Reservation, RecurringBooking, MonthlyBill, seed_data,
    FORMAS_PAGAMENTO, STATUS_RESERVA, BILLING_TYPES, STATUS_RECORRENTE, DIAS_SEMANA
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "pdf", "webp", "heic"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'reservas.db')}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB

db.init_app(app)

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Faça login para acessar o sistema."
login_manager.init_app(app)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------- Auth ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            next_url = request.args.get("next")
            return redirect(next_url or url_for("dashboard"))
        flash("E-mail ou senha inválidos.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------- Dashboard ----------

@app.route("/")
@login_required
def dashboard():
    topup_recurring_bookings()

    today = date.today().isoformat()
    proximas = (
        Reservation.query.filter(Reservation.date >= today, Reservation.status != "Cancelada")
        .order_by(Reservation.date.asc(), Reservation.start_time.asc())
        .limit(8)
        .all()
    )
    pendentes_pagamento = (
        Reservation.query.filter_by(
            is_paid=True, payment_confirmed=False, recurring_booking_id=None
        )
        .order_by(Reservation.date.asc())
        .all()
    )
    mes_atual = date.today().strftime("%Y-%m")
    faturas_pendentes = (
        MonthlyBill.query.filter(
            MonthlyBill.payment_confirmed == False,  # noqa: E712
            MonthlyBill.month <= mes_atual,
        )
        .order_by(MonthlyBill.month.asc())
        .all()
    )
    total_espacos = Space.query.filter_by(active=True).count()
    total_reservas_mes = Reservation.query.filter(
        Reservation.date >= date.today().replace(day=1).isoformat()
    ).count()
    total_fixas_ativas = RecurringBooking.query.filter_by(status="Ativa").count()
    return render_template(
        "dashboard.html",
        proximas=proximas,
        pendentes_pagamento=pendentes_pagamento,
        faturas_pendentes=faturas_pendentes,
        total_espacos=total_espacos,
        total_reservas_mes=total_reservas_mes,
        total_fixas_ativas=total_fixas_ativas,
    )


# ---------- Calendário ----------

@app.route("/calendario")
@login_required
def calendario():
    spaces = Space.query.order_by(Space.name).all()
    return render_template("calendario.html", spaces=spaces)


@app.route("/api/reservas")
@login_required
def api_reservas():
    space_id = request.args.get("space_id", type=int)
    query = Reservation.query.filter(Reservation.status != "Cancelada")
    if space_id:
        query = query.filter_by(space_id=space_id)
    events = []
    for r in query.all():
        title = f"{r.space.name} - {r.requester_name}"
        if r.ministry:
            title += f" ({r.ministry.name})"
        events.append({
            "id": r.id,
            "title": title,
            "start": f"{r.date}T{r.start_time}",
            "end": f"{r.date}T{r.end_time}",
            "color": r.space.color,
            "url": url_for("reserva_detail", reserva_id=r.id),
        })
    return jsonify(events)


# ---------- Reservas ----------

def check_conflict(space_id, date_str, start_time, end_time, exclude_id=None):
    query = Reservation.query.filter(
        Reservation.space_id == space_id,
        Reservation.date == date_str,
        Reservation.status != "Cancelada",
    )
    if exclude_id:
        query = query.filter(Reservation.id != exclude_id)
    for r in query.all():
        if start_time < r.end_time and end_time > r.start_time:
            return r
    return None


# Quantos dias para frente a agenda de uma reserva fixa é gerada de cada vez.
RECURRING_GENERATE_DAYS = 90
# Quando faltar menos que isso de agenda gerada, estende automaticamente.
RECURRING_TOPUP_THRESHOLD_DAYS = 30


def generate_recurring_occurrences(recurring, months_ahead_days=RECURRING_GENERATE_DAYS):
    """Cria as reservas (Reservation) individuais e as faturas mensais (MonthlyBill)
    de uma reserva fixa, a partir da última ocorrência já gerada (ou start_date),
    até `months_ahead_days` no futuro. Pula datas com conflito e avisa quais."""
    if recurring.status != "Ativa":
        return [], []

    weekdays = set(recurring.weekdays_list())
    horizon = date.today() + timedelta(days=months_ahead_days)

    last = (
        Reservation.query.filter_by(recurring_booking_id=recurring.id)
        .order_by(Reservation.date.desc())
        .first()
    )
    if last:
        cursor = datetime.strptime(last.date, "%Y-%m-%d").date() + timedelta(days=1)
    else:
        cursor = datetime.strptime(recurring.start_date, "%Y-%m-%d").date()

    created = []
    conflicts = []
    months_touched = set()

    while cursor <= horizon:
        if cursor.weekday() in weekdays:
            date_str = cursor.isoformat()
            conflict = check_conflict(recurring.space_id, date_str, recurring.start_time, recurring.end_time)
            if conflict:
                conflicts.append(date_str)
            else:
                r = Reservation(
                    space_id=recurring.space_id,
                    ministry_id=recurring.ministry_id,
                    requester_name=recurring.requester_name,
                    requester_contact=recurring.requester_contact,
                    activity=recurring.activity,
                    date=date_str,
                    start_time=recurring.start_time,
                    end_time=recurring.end_time,
                    is_paid=False,
                    status="Confirmada",
                    notes="Gerada automaticamente por reserva fixa. O pagamento (se houver) é controlado pela cobrança mensal, não por esta ocorrência.",
                    created_by=recurring.created_by,
                    recurring_booking_id=recurring.id,
                )
                db.session.add(r)
                created.append(date_str)
                months_touched.add(cursor.strftime("%Y-%m"))
        cursor += timedelta(days=1)

    if recurring.billing_type == "Mensal":
        for month in months_touched:
            exists = MonthlyBill.query.filter_by(recurring_booking_id=recurring.id, month=month).first()
            if not exists:
                db.session.add(MonthlyBill(
                    recurring_booking_id=recurring.id,
                    month=month,
                    value=recurring.monthly_value,
                    payment_method=recurring.payment_method,
                ))

    db.session.commit()
    return created, conflicts


def topup_recurring_bookings():
    """Verifica todas as reservas fixas ativas e estende a agenda gerada
    quando estiver acabando. Seguro de chamar em toda visita ao painel."""
    ativos = RecurringBooking.query.filter_by(status="Ativa").all()
    for recurring in ativos:
        last = (
            Reservation.query.filter_by(recurring_booking_id=recurring.id)
            .order_by(Reservation.date.desc())
            .first()
        )
        dias_restantes = None
        if last:
            dias_restantes = (datetime.strptime(last.date, "%Y-%m-%d").date() - date.today()).days
        if last is None or dias_restantes is not None and dias_restantes < RECURRING_TOPUP_THRESHOLD_DAYS:
            generate_recurring_occurrences(recurring)


@app.route("/reservas")
@login_required
def reservas_list():
    query = Reservation.query
    space_id = request.args.get("space_id", type=int)
    ministry_id = request.args.get("ministry_id", type=int)
    status = request.args.get("status", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")

    if space_id:
        query = query.filter_by(space_id=space_id)
    if ministry_id:
        query = query.filter_by(ministry_id=ministry_id)
    if status:
        query = query.filter_by(status=status)
    if date_from:
        query = query.filter(Reservation.date >= date_from)
    if date_to:
        query = query.filter(Reservation.date <= date_to)

    reservas = query.order_by(Reservation.date.desc(), Reservation.start_time.asc()).all()
    spaces = Space.query.order_by(Space.name).all()
    ministries = Ministry.query.order_by(Ministry.name).all()
    return render_template(
        "reservas_list.html",
        reservas=reservas, spaces=spaces, ministries=ministries,
        status_options=STATUS_RESERVA,
        filters=request.args,
    )


@app.route("/reservas/anteriores")
@login_required
def reservas_anteriores():
    ontem = (date.today() - timedelta(days=1)).isoformat()
    return redirect(url_for("reservas_list", date_to=ontem, historico=1))


@app.route("/reservas/nova", methods=["GET", "POST"])
@login_required
def reserva_nova():
    spaces = Space.query.filter_by(active=True).order_by(Space.name).all()
    ministries = Ministry.query.filter_by(active=True).order_by(Ministry.name).all()

    if request.method == "POST":
        form = request.form
        space_id = form.get("space_id", type=int)
        date_str = form.get("date", "")
        start_time = form.get("start_time", "")
        end_time = form.get("end_time", "")

        errors = []
        if not space_id:
            errors.append("Selecione um espaço.")
        if not date_str:
            errors.append("Informe a data.")
        if not start_time or not end_time:
            errors.append("Informe o horário de início e fim.")
        elif start_time >= end_time:
            errors.append("O horário de término deve ser depois do início.")
        if not form.get("requester_name", "").strip():
            errors.append("Informe o nome do responsável.")

        conflict = None
        if space_id and date_str and start_time and end_time and start_time < end_time:
            conflict = check_conflict(space_id, date_str, start_time, end_time)
            if conflict:
                errors.append(
                    f"Conflito de horário: {conflict.space.name} já está reservado por "
                    f"{conflict.requester_name} das {conflict.start_time} às {conflict.end_time} nesse dia."
                )

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template(
                "reserva_form.html", spaces=spaces, ministries=ministries,
                formas=FORMAS_PAGAMENTO, status_options=STATUS_RESERVA,
                reserva=None, form=form,
            )

        is_paid = form.get("is_paid") == "on"
        proof_filename = ""
        file = request.files.get("payment_proof")
        if is_paid and file and file.filename and allowed_file(file.filename):
            filename = secure_filename(f"{datetime.utcnow().timestamp()}_{file.filename}")
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            proof_filename = filename

        reserva = Reservation(
            space_id=space_id,
            ministry_id=form.get("ministry_id", type=int) or None,
            requester_name=form.get("requester_name", "").strip(),
            requester_contact=form.get("requester_contact", "").strip(),
            activity=form.get("activity", "").strip(),
            date=date_str,
            start_time=start_time,
            end_time=end_time,
            is_paid=is_paid,
            payment_method=form.get("payment_method", "") if is_paid else "",
            payment_value=float(form.get("payment_value") or 0) if is_paid else 0.0,
            payment_proof_filename=proof_filename,
            payment_confirmed=form.get("payment_confirmed") == "on",
            status=form.get("status", "Confirmada"),
            notes=form.get("notes", "").strip(),
            created_by=current_user.name,
        )
        db.session.add(reserva)
        db.session.commit()
        flash("Reserva criada com sucesso.", "success")
        return redirect(url_for("reserva_detail", reserva_id=reserva.id))

    prefill_date = request.args.get("date", "")
    prefill_space = request.args.get("space_id", "")
    return render_template(
        "reserva_form.html", spaces=spaces, ministries=ministries,
        formas=FORMAS_PAGAMENTO, status_options=STATUS_RESERVA,
        reserva=None,
        form={"date": prefill_date, "space_id": prefill_space, "status": "Confirmada"},
    )


@app.route("/reservas/<int:reserva_id>")
@login_required
def reserva_detail(reserva_id):
    reserva = Reservation.query.get_or_404(reserva_id)
    return render_template("reserva_detail.html", reserva=reserva)


@app.route("/reservas/<int:reserva_id>/editar", methods=["GET", "POST"])
@login_required
def reserva_editar(reserva_id):
    reserva = Reservation.query.get_or_404(reserva_id)
    spaces = Space.query.order_by(Space.name).all()
    ministries = Ministry.query.order_by(Ministry.name).all()

    if request.method == "POST":
        form = request.form
        space_id = form.get("space_id", type=int)
        date_str = form.get("date", "")
        start_time = form.get("start_time", "")
        end_time = form.get("end_time", "")

        errors = []
        if not space_id:
            errors.append("Selecione um espaço.")
        if not date_str:
            errors.append("Informe a data.")
        if not start_time or not end_time or start_time >= end_time:
            errors.append("Horário inválido.")
        if not form.get("requester_name", "").strip():
            errors.append("Informe o nome do responsável.")

        conflict = None
        if space_id and date_str and start_time and end_time and start_time < end_time:
            conflict = check_conflict(space_id, date_str, start_time, end_time, exclude_id=reserva.id)
            if conflict:
                errors.append(
                    f"Conflito de horário: {conflict.space.name} já está reservado por "
                    f"{conflict.requester_name} das {conflict.start_time} às {conflict.end_time} nesse dia."
                )

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template(
                "reserva_form.html", spaces=spaces, ministries=ministries,
                formas=FORMAS_PAGAMENTO, status_options=STATUS_RESERVA,
                reserva=reserva, form=form,
            )

        is_paid = form.get("is_paid") == "on"
        file = request.files.get("payment_proof")
        if is_paid and file and file.filename and allowed_file(file.filename):
            filename = secure_filename(f"{datetime.utcnow().timestamp()}_{file.filename}")
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            reserva.payment_proof_filename = filename

        reserva.space_id = space_id
        reserva.ministry_id = form.get("ministry_id", type=int) or None
        reserva.requester_name = form.get("requester_name", "").strip()
        reserva.requester_contact = form.get("requester_contact", "").strip()
        reserva.activity = form.get("activity", "").strip()
        reserva.date = date_str
        reserva.start_time = start_time
        reserva.end_time = end_time
        reserva.is_paid = is_paid
        reserva.payment_method = form.get("payment_method", "") if is_paid else ""
        reserva.payment_value = float(form.get("payment_value") or 0) if is_paid else 0.0
        reserva.payment_confirmed = form.get("payment_confirmed") == "on"
        reserva.status = form.get("status", "Confirmada")
        reserva.notes = form.get("notes", "").strip()

        db.session.commit()
        flash("Reserva atualizada.", "success")
        return redirect(url_for("reserva_detail", reserva_id=reserva.id))

    return render_template(
        "reserva_form.html", spaces=spaces, ministries=ministries,
        formas=FORMAS_PAGAMENTO, status_options=STATUS_RESERVA,
        reserva=reserva, form=None,
    )


@app.route("/reservas/<int:reserva_id>/excluir", methods=["POST"])
@login_required
def reserva_excluir(reserva_id):
    reserva = Reservation.query.get_or_404(reserva_id)
    db.session.delete(reserva)
    db.session.commit()
    flash("Reserva excluída.", "info")
    return redirect(url_for("reservas_list"))


@app.route("/uploads/<path:filename>")
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ---------- Espaços ----------

@app.route("/espacos")
@login_required
def espacos_list():
    spaces = Space.query.order_by(Space.name).all()
    return render_template("espacos.html", spaces=spaces)


@app.route("/espacos/novo", methods=["GET", "POST"])
@login_required
def espaco_novo():
    if request.method == "POST":
        form = request.form
        space = Space(
            name=form.get("name", "").strip(),
            space_type=form.get("space_type", "Outro"),
            activities=form.get("activities", "").strip(),
            color=form.get("color", "#2563eb"),
            notes=form.get("notes", "").strip(),
            active=True,
        )
        if not space.name:
            flash("Informe o nome do espaço.", "danger")
            return render_template("espaco_form.html", space=None)
        db.session.add(space)
        db.session.commit()
        flash("Espaço cadastrado.", "success")
        return redirect(url_for("espacos_list"))
    return render_template("espaco_form.html", space=None)


@app.route("/espacos/<int:space_id>/editar", methods=["GET", "POST"])
@login_required
def espaco_editar(space_id):
    space = Space.query.get_or_404(space_id)
    if request.method == "POST":
        form = request.form
        space.name = form.get("name", "").strip()
        space.space_type = form.get("space_type", "Outro")
        space.activities = form.get("activities", "").strip()
        space.color = form.get("color", "#2563eb")
        space.notes = form.get("notes", "").strip()
        space.active = form.get("active") == "on"
        db.session.commit()
        flash("Espaço atualizado.", "success")
        return redirect(url_for("espacos_list"))
    return render_template("espaco_form.html", space=space)


@app.route("/espacos/<int:space_id>/excluir", methods=["POST"])
@login_required
def espaco_excluir(space_id):
    space = Space.query.get_or_404(space_id)
    if space.reservations:
        flash("Não é possível excluir: este espaço já tem reservas. Desative-o em vez disso.", "danger")
        return redirect(url_for("espacos_list"))
    db.session.delete(space)
    db.session.commit()
    flash("Espaço excluído.", "info")
    return redirect(url_for("espacos_list"))


# ---------- Ministérios ----------

@app.route("/ministerios")
@login_required
def ministerios_list():
    ministries = Ministry.query.order_by(Ministry.name).all()
    return render_template("ministerios.html", ministries=ministries)


@app.route("/ministerios/novo", methods=["GET", "POST"])
@login_required
def ministerio_novo():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Informe o nome do ministério.", "danger")
            return render_template("ministerio_form.html", ministry=None)
        if Ministry.query.filter_by(name=name).first():
            flash("Já existe um ministério com esse nome.", "danger")
            return render_template("ministerio_form.html", ministry=None)
        db.session.add(Ministry(name=name))
        db.session.commit()
        flash("Ministério cadastrado.", "success")
        return redirect(url_for("ministerios_list"))
    return render_template("ministerio_form.html", ministry=None)


@app.route("/ministerios/<int:ministry_id>/editar", methods=["GET", "POST"])
@login_required
def ministerio_editar(ministry_id):
    ministry = Ministry.query.get_or_404(ministry_id)
    if request.method == "POST":
        ministry.name = request.form.get("name", "").strip()
        ministry.active = request.form.get("active") == "on"
        db.session.commit()
        flash("Ministério atualizado.", "success")
        return redirect(url_for("ministerios_list"))
    return render_template("ministerio_form.html", ministry=ministry)


@app.route("/ministerios/<int:ministry_id>/excluir", methods=["POST"])
@login_required
def ministerio_excluir(ministry_id):
    ministry = Ministry.query.get_or_404(ministry_id)
    if ministry.reservations:
        flash("Não é possível excluir: este ministério já tem reservas. Desative-o em vez disso.", "danger")
        return redirect(url_for("ministerios_list"))
    db.session.delete(ministry)
    db.session.commit()
    flash("Ministério excluído.", "info")
    return redirect(url_for("ministerios_list"))


# ---------- Reservas fixas / recorrentes ----------

@app.route("/recorrentes")
@login_required
def recorrentes_list():
    topup_recurring_bookings()
    recorrentes = RecurringBooking.query.order_by(
        RecurringBooking.status.asc(), RecurringBooking.requester_name.asc()
    ).all()
    return render_template("recorrentes_list.html", recorrentes=recorrentes)


@app.route("/recorrentes/nova", methods=["GET", "POST"])
@login_required
def recorrente_nova():
    spaces = Space.query.filter_by(active=True).order_by(Space.name).all()
    ministries = Ministry.query.filter_by(active=True).order_by(Ministry.name).all()

    if request.method == "POST":
        form = request.form
        space_id = form.get("space_id", type=int)
        dias = form.getlist("weekdays")
        start_time = form.get("start_time", "")
        end_time = form.get("end_time", "")
        start_date = form.get("start_date", "")
        billing_type = form.get("billing_type", "Gratuita")
        monthly_value = form.get("monthly_value", "")

        errors = []
        if not space_id:
            errors.append("Selecione um espaço.")
        if not dias:
            errors.append("Selecione ao menos um dia da semana.")
        if not start_time or not end_time or start_time >= end_time:
            errors.append("Horário inválido.")
        if not start_date:
            errors.append("Informe a data de início.")
        if not form.get("requester_name", "").strip():
            errors.append("Informe o nome do responsável.")
        if billing_type == "Mensal" and (not monthly_value or float(monthly_value or 0) <= 0):
            errors.append("Informe o valor da mensalidade.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template(
                "recorrente_form.html", spaces=spaces, ministries=ministries,
                formas=FORMAS_PAGAMENTO, dias_semana=DIAS_SEMANA,
                recorrente=None, form=form,
            )

        recurring = RecurringBooking(
            space_id=space_id,
            ministry_id=form.get("ministry_id", type=int) or None,
            requester_name=form.get("requester_name", "").strip(),
            requester_contact=form.get("requester_contact", "").strip(),
            activity=form.get("activity", "").strip(),
            weekdays=",".join(dias),
            start_time=start_time,
            end_time=end_time,
            start_date=start_date,
            billing_type=billing_type,
            monthly_value=float(monthly_value) if billing_type == "Mensal" and monthly_value else 0.0,
            payment_method=form.get("payment_method", "") if billing_type == "Mensal" else "",
            notes=form.get("notes", "").strip(),
            created_by=current_user.name,
        )
        db.session.add(recurring)
        db.session.commit()

        created, conflicts = generate_recurring_occurrences(recurring)
        flash(f"Reserva fixa criada. {len(created)} ocorrência(s) geradas para os próximos meses.", "success")
        if conflicts:
            flash(
                f"Atenção: {len(conflicts)} data(s) tiveram conflito com outra reserva e foram puladas: "
                + ", ".join(conflicts[:10]) + ("..." if len(conflicts) > 10 else ""),
                "danger",
            )
        return redirect(url_for("recorrente_detail", recorrente_id=recurring.id))

    return render_template(
        "recorrente_form.html", spaces=spaces, ministries=ministries,
        formas=FORMAS_PAGAMENTO, dias_semana=DIAS_SEMANA,
        recorrente=None, form=None,
    )


@app.route("/recorrentes/<int:recorrente_id>")
@login_required
def recorrente_detail(recorrente_id):
    recurring = RecurringBooking.query.get_or_404(recorrente_id)
    hoje = date.today().isoformat()
    proximas = [r for r in recurring.reservations if r.date >= hoje and r.status != "Cancelada"]
    passadas = [r for r in recurring.reservations if r.date < hoje]
    bills = sorted(recurring.bills, key=lambda b: b.month, reverse=True)
    return render_template(
        "recorrente_detail.html", recurring=recurring,
        proximas=proximas, passadas=passadas, bills=bills,
    )


@app.route("/recorrentes/<int:recorrente_id>/encerrar", methods=["POST"])
@login_required
def recorrente_encerrar(recorrente_id):
    recurring = RecurringBooking.query.get_or_404(recorrente_id)
    recurring.status = "Encerrada"
    hoje = date.today().isoformat()
    canceladas = 0
    for r in recurring.reservations:
        if r.date >= hoje and r.status != "Cancelada":
            r.status = "Cancelada"
            canceladas += 1
    db.session.commit()
    flash(f"Reserva fixa encerrada. {canceladas} ocorrência(s) futuras foram canceladas.", "info")
    return redirect(url_for("recorrente_detail", recorrente_id=recurring.id))


@app.route("/recorrentes/<int:recorrente_id>/faturas/<int:bill_id>/pagar", methods=["GET", "POST"])
@login_required
def fatura_pagar(recorrente_id, bill_id):
    recurring = RecurringBooking.query.get_or_404(recorrente_id)
    bill = MonthlyBill.query.get_or_404(bill_id)
    if bill.recurring_booking_id != recurring.id:
        abort(404)

    if request.method == "POST":
        bill.payment_method = request.form.get("payment_method", bill.payment_method)
        bill.value = float(request.form.get("value") or bill.value)
        bill.payment_confirmed = request.form.get("payment_confirmed") == "on"
        bill.paid_at = date.today().isoformat() if bill.payment_confirmed else ""
        bill.notes = request.form.get("notes", "").strip()

        file = request.files.get("payment_proof")
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(f"{datetime.utcnow().timestamp()}_{file.filename}")
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            bill.payment_proof_filename = filename

        db.session.commit()
        flash("Fatura atualizada.", "success")
        return redirect(url_for("recorrente_detail", recorrente_id=recurring.id))

    return render_template(
        "fatura_form.html", recurring=recurring, bill=bill, formas=FORMAS_PAGAMENTO,
    )


# ---------- Conta ----------

@app.route("/conta/senha", methods=["GET", "POST"])
@login_required
def alterar_senha():
    if request.method == "POST":
        atual = request.form.get("senha_atual", "")
        nova = request.form.get("senha_nova", "")
        confirma = request.form.get("senha_confirma", "")
        if not current_user.check_password(atual):
            flash("Senha atual incorreta.", "danger")
        elif len(nova) < 6:
            flash("A nova senha deve ter pelo menos 6 caracteres.", "danger")
        elif nova != confirma:
            flash("As senhas não coincidem.", "danger")
        else:
            current_user.set_password(nova)
            db.session.commit()
            flash("Senha alterada com sucesso.", "success")
            return redirect(url_for("dashboard"))
    return render_template("alterar_senha.html")


# ---------- Usuários (gestores) ----------

@app.route("/usuarios")
@login_required
def usuarios_list():
    usuarios = User.query.order_by(User.name).all()
    return render_template("usuarios.html", usuarios=usuarios)


@app.route("/usuarios/novo", methods=["GET", "POST"])
@login_required
def usuario_novo():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        senha_confirma = request.form.get("senha_confirma", "")

        errors = []
        if not name:
            errors.append("Informe o nome.")
        if not email:
            errors.append("Informe o e-mail.")
        elif User.query.filter_by(email=email).first():
            errors.append("Já existe um usuário com esse e-mail.")
        if len(senha) < 6:
            errors.append("A senha deve ter pelo menos 6 caracteres.")
        elif senha != senha_confirma:
            errors.append("As senhas não coincidem.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("usuario_form.html", usuario=None, form=request.form)

        novo = User(name=name, email=email)
        novo.set_password(senha)
        db.session.add(novo)
        db.session.commit()
        flash(f"Usuário {name} criado com sucesso.", "success")
        return redirect(url_for("usuarios_list"))

    return render_template("usuario_form.html", usuario=None, form=None)


@app.route("/usuarios/<int:user_id>/editar", methods=["GET", "POST"])
@login_required
def usuario_editar(user_id):
    usuario = User.query.get_or_404(user_id)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        senha = request.form.get("senha", "")
        senha_confirma = request.form.get("senha_confirma", "")

        errors = []
        if not name:
            errors.append("Informe o nome.")
        if not email:
            errors.append("Informe o e-mail.")
        else:
            existente = User.query.filter_by(email=email).first()
            if existente and existente.id != usuario.id:
                errors.append("Já existe um usuário com esse e-mail.")
        if senha or senha_confirma:
            if len(senha) < 6:
                errors.append("A nova senha deve ter pelo menos 6 caracteres.")
            elif senha != senha_confirma:
                errors.append("As senhas não coincidem.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("usuario_form.html", usuario=usuario, form=request.form)

        usuario.name = name
        usuario.email = email
        if senha:
            usuario.set_password(senha)
        db.session.commit()
        flash("Usuário atualizado.", "success")
        return redirect(url_for("usuarios_list"))

    return render_template("usuario_form.html", usuario=usuario, form=None)


@app.route("/usuarios/<int:user_id>/excluir", methods=["POST"])
@login_required
def usuario_excluir(user_id):
    usuario = User.query.get_or_404(user_id)
    if usuario.id == current_user.id:
        flash("Você não pode excluir o próprio usuário enquanto estiver logado com ele.", "danger")
        return redirect(url_for("usuarios_list"))
    if User.query.count() <= 1:
        flash("Não é possível excluir o único usuário do sistema.", "danger")
        return redirect(url_for("usuarios_list"))
    db.session.delete(usuario)
    db.session.commit()
    flash("Usuário excluído.", "info")
    return redirect(url_for("usuarios_list"))


def run_light_migrations():
    """Adiciona colunas/tabelas novas em bancos já existentes, sem apagar dados.
    (Alternativa simples a uma ferramenta de migração completa.)"""
    inspector = inspect(db.engine)
    if inspector.has_table("reservation"):
        cols = {c["name"] for c in inspector.get_columns("reservation")}
        if "recurring_booking_id" not in cols:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE reservation ADD COLUMN recurring_booking_id INTEGER"))
                conn.commit()


with app.app_context():
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    db.create_all()
    run_light_migrations()
    seed_data()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
