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

    inicio_mes = date.today().replace(day=1)
    prox_mes = (inicio_mes.replace(day=28) + timedelta(days=4)).replace(day=1)
    fim_mes = prox_mes - timedelta(days=1)
    total_reservas_mes = Reservation.query.filter(
        Reservation.date >= inicio_mes.isoformat(),
        Reservation.date <= fim_mes.isoformat(),
        Reservation.status != "Cancelada",
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

def space_occupied_ids(space):
    """IDs de todos os espaços fisicamente ocupados quando `space` é reservado.
    Espaços "pacote" (ex.: Pátio Completo) ocupam os espaços que os compõem."""
    if space and space.is_composite:
        ids = space.composite_ids_list()
        return set(ids) if ids else {space.id}
    return {space.id} if space else set()


def check_conflict(space_id, date_str, start_time, end_time, exclude_id=None):
    space = Space.query.get(space_id)
    afetados = space_occupied_ids(space)

    query = Reservation.query.filter(
        Reservation.date == date_str,
        Reservation.status != "Cancelada",
    )
    if exclude_id:
        query = query.filter(Reservation.id != exclude_id)
    for r in query.all():
        if start_time < r.end_time and end_time > r.start_time:
            if afetados & space_occupied_ids(r.space):
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
    periodo = request.args.get("periodo", "")
    historico = request.args.get("historico", "")

    if space_id:
        query = query.filter_by(space_id=space_id)
    if ministry_id:
        query = query.filter_by(ministry_id=ministry_id)
    if status:
        query = query.filter_by(status=status)
    else:
        # Por padrão, reservas canceladas não poluem a tela de reservas.
        # Se o gestor quiser vê-las, pode selecionar "Cancelada" no filtro de status.
        query = query.filter(Reservation.status != "Cancelada")

    hoje = date.today()
    if periodo == "mes_atual":
        inicio_mes = hoje.replace(day=1)
        prox_mes = (inicio_mes.replace(day=28) + timedelta(days=4)).replace(day=1)
        date_from = inicio_mes.isoformat()
        date_to = (prox_mes - timedelta(days=1)).isoformat()
    elif periodo == "proximo_mes":
        inicio_mes = hoje.replace(day=1)
        prox_mes = (inicio_mes.replace(day=28) + timedelta(days=4)).replace(day=1)
        depois = (prox_mes.replace(day=28) + timedelta(days=4)).replace(day=1)
        date_from = prox_mes.isoformat()
        date_to = (depois - timedelta(days=1)).isoformat()

    if date_from:
        query = query.filter(Reservation.date >= date_from)
    if date_to:
        query = query.filter(Reservation.date <= date_to)

    reservas_raw = query.order_by(Reservation.date.desc(), Reservation.start_time.asc()).all()

    if historico:
        # No histórico mostramos cada ocorrência normalmente (registro do que aconteceu).
        reservas = reservas_raw
    else:
        # Na tela de Reservas, agrupamos as ocorrências de uma mesma reserva fixa
        # em um único item (a próxima data), para não poluir a tela.
        hoje_str = hoje.isoformat()
        avulsas = [r for r in reservas_raw if not r.recurring_booking_id]
        por_recorrente = {}
        for r in reservas_raw:
            if r.recurring_booking_id:
                por_recorrente.setdefault(r.recurring_booking_id, []).append(r)
        representantes = []
        for ocorrencias in por_recorrente.values():
            futuras = [o for o in ocorrencias if o.date >= hoje_str]
            escolhida = min(futuras, key=lambda o: o.date) if futuras else max(ocorrencias, key=lambda o: o.date)
            representantes.append(escolhida)
        reservas = sorted(avulsas + representantes, key=lambda r: (r.date, r.start_time), reverse=True)

    spaces = Space.query.order_by(Space.name).all()
    ministries = Ministry.query.order_by(Ministry.name).all()

    filters_display = dict(request.args)
    filters_display["date_from"] = date_from
    filters_display["date_to"] = date_to

    return render_template(
        "reservas_list.html",
        reservas=reservas, spaces=spaces, ministries=ministries,
        status_options=STATUS_RESERVA,
        filters=request.args, filters_display=filters_display,
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
            payment_paid_at=date.today().isoformat() if form.get("payment_confirmed") == "on" else "",
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
        novo_confirmado = form.get("payment_confirmed") == "on"
        if novo_confirmado and not reserva.payment_confirmed:
            reserva.payment_paid_at = date.today().isoformat()
        elif not novo_confirmado:
            reserva.payment_paid_at = ""
        reserva.payment_confirmed = novo_confirmado
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
    outros_espacos = Space.query.order_by(Space.name).all()
    if request.method == "POST":
        form = request.form
        is_composite = form.get("is_composite") == "on"
        composite_ids = form.getlist("composite_of") if is_composite else []
        space = Space(
            name=form.get("name", "").strip(),
            space_type=form.get("space_type", "Outro"),
            activities=form.get("activities", "").strip(),
            color=form.get("color", "#2563eb"),
            notes=form.get("notes", "").strip(),
            active=True,
            is_composite=is_composite,
            composite_of=",".join(composite_ids),
        )
        if not space.name:
            flash("Informe o nome do espaço.", "danger")
            return render_template("espaco_form.html", space=None, outros_espacos=outros_espacos)
        if is_composite and not composite_ids:
            flash("Selecione ao menos um espaço que este pacote ocupa.", "danger")
            return render_template("espaco_form.html", space=None, outros_espacos=outros_espacos)
        db.session.add(space)
        db.session.commit()
        flash("Espaço cadastrado.", "success")
        return redirect(url_for("espacos_list"))
    return render_template("espaco_form.html", space=None, outros_espacos=outros_espacos)


@app.route("/espacos/<int:space_id>/editar", methods=["GET", "POST"])
@login_required
def espaco_editar(space_id):
    space = Space.query.get_or_404(space_id)
    outros_espacos = Space.query.filter(Space.id != space_id).order_by(Space.name).all()
    if request.method == "POST":
        form = request.form
        is_composite = form.get("is_composite") == "on"
        composite_ids = form.getlist("composite_of") if is_composite else []
        if is_composite and not composite_ids:
            flash("Selecione ao menos um espaço que este pacote ocupa.", "danger")
            return render_template("espaco_form.html", space=space, outros_espacos=outros_espacos)
        space.name = form.get("name", "").strip()
        space.space_type = form.get("space_type", "Outro")
        space.activities = form.get("activities", "").strip()
        space.color = form.get("color", "#2563eb")
        space.notes = form.get("notes", "").strip()
        space.active = form.get("active") == "on"
        space.is_composite = is_composite
        space.composite_of = ",".join(composite_ids)
        db.session.commit()
        flash("Espaço atualizado.", "success")
        return redirect(url_for("espacos_list"))
    return render_template("espaco_form.html", space=space, outros_espacos=outros_espacos)


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


@app.route("/cobrancas")
@login_required
def cobrancas_list():
    topup_recurring_bookings()
    view = request.args.get("view", "pendentes")
    pagas = (view == "historico")

    itens = []

    bills = (MonthlyBill.query.join(RecurringBooking)
             .filter(MonthlyBill.payment_confirmed == pagas)  # noqa: E712
             .all())
    for b in bills:
        itens.append({
            "tipo": "mensal",
            "tipo_label": "Fixa · Mensal",
            "responsavel": b.recurring_booking.requester_name,
            "space": b.recurring_booking.space,
            "referencia": b.month,
            "valor": b.value,
            "payment_method": b.payment_method,
            "pago": b.payment_confirmed,
            "pago_em": b.paid_at,
            "comprovante": b.payment_proof_filename,
            "link_dar_baixa": url_for("fatura_pagar", recorrente_id=b.recurring_booking_id, bill_id=b.id),
            "link_editar": url_for("fatura_pagar", recorrente_id=b.recurring_booking_id, bill_id=b.id),
            "link_ver": url_for("recorrente_detail", recorrente_id=b.recurring_booking_id),
            "acao_rapida": False,
            "ordenacao": b.month,
        })

    avulsas = (Reservation.query
               .filter_by(is_paid=True, recurring_booking_id=None, payment_confirmed=pagas)
               .all())
    for r in avulsas:
        itens.append({
            "tipo": "avulsa",
            "tipo_label": "Avulsa",
            "responsavel": r.requester_name,
            "space": r.space,
            "referencia": r.date,
            "valor": r.payment_value,
            "payment_method": r.payment_method,
            "pago": r.payment_confirmed,
            "pago_em": r.payment_paid_at,
            "comprovante": r.payment_proof_filename,
            "link_dar_baixa": url_for("reserva_dar_baixa", reserva_id=r.id),
            "link_editar": url_for("reserva_editar", reserva_id=r.id),
            "link_ver": url_for("reserva_detail", reserva_id=r.id),
            "acao_rapida": True,
            "ordenacao": r.date,
        })

    itens.sort(key=lambda i: i["ordenacao"], reverse=True)
    total = sum(i["valor"] for i in itens)

    return render_template("cobrancas.html", itens=itens, view=view, total=total)


@app.route("/reservas/<int:reserva_id>/dar-baixa", methods=["POST"])
@login_required
def reserva_dar_baixa(reserva_id):
    reserva = Reservation.query.get_or_404(reserva_id)
    if not reserva.is_paid:
        flash("Esta reserva não está marcada como paga.", "warning")
    elif reserva.payment_confirmed:
        flash("Este pagamento já estava confirmado.", "info")
    else:
        reserva.payment_confirmed = True
        reserva.payment_paid_at = date.today().isoformat()
        db.session.commit()
        flash("Pagamento dado como recebido.", "success")
    return redirect(request.referrer or url_for("cobrancas_list"))


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
        with db.engine.connect() as conn:
            if "recurring_booking_id" not in cols:
                conn.execute(text("ALTER TABLE reservation ADD COLUMN recurring_booking_id INTEGER"))
            if "payment_paid_at" not in cols:
                conn.execute(text("ALTER TABLE reservation ADD COLUMN payment_paid_at VARCHAR(10) DEFAULT ''"))
            conn.commit()
    if inspector.has_table("space"):
        cols = {c["name"] for c in inspector.get_columns("space")}
        with db.engine.connect() as conn:
            if "is_composite" not in cols:
                conn.execute(text("ALTER TABLE space ADD COLUMN is_composite BOOLEAN DEFAULT 0"))
            if "composite_of" not in cols:
                conn.execute(text("ALTER TABLE space ADD COLUMN composite_of VARCHAR(255) DEFAULT ''"))
            conn.commit()


def ensure_composite_space():
    """Garante a existência do espaço 'Pátio Completo': ao ser reservado, ocupa
    Quadra de Cimento, Quadra de Areia e Churrasqueira ao mesmo tempo."""
    if Space.query.filter_by(name="Pátio Completo").first():
        return
    nomes = ["Quadra de Cimento", "Quadra de Areia", "Churrasqueira"]
    componentes = Space.query.filter(Space.name.in_(nomes)).all()
    if not componentes:
        return
    ids = ",".join(str(s.id) for s in componentes)
    atividades = sorted({a for s in componentes for a in s.activities_list()})
    nomes_incluidos = ", ".join(s.name for s in componentes)
    patio = Space(
        name="Pátio Completo",
        space_type="Outro",
        activities=", ".join(atividades),
        color="#2b2f2e",
        active=True,
        notes=f"Reserva conjunta: ocupa {nomes_incluidos} ao mesmo tempo.",
        is_composite=True,
        composite_of=ids,
    )
    db.session.add(patio)
    db.session.commit()


with app.app_context():
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    db.create_all()
    run_light_migrations()
    seed_data()
    ensure_composite_space()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
