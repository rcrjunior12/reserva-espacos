from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

FORMAS_PAGAMENTO = ["Dinheiro", "PIX", "Cartão de crédito", "Cartão de débito", "Transferência bancária", "Outro"]
STATUS_RESERVA = ["Confirmada", "Pendente", "Cancelada"]
BILLING_TYPES = ["Gratuita", "Mensal"]
STATUS_RECORRENTE = ["Ativa", "Encerrada"]
DIAS_SEMANA = [
    ("0", "Segunda"), ("1", "Terça"), ("2", "Quarta"), ("3", "Quinta"),
    ("4", "Sexta"), ("5", "Sábado"), ("6", "Domingo"),
]


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Space(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    space_type = db.Column(db.String(50), nullable=False, default="Outro")
    activities = db.Column(db.String(255), default="")  # comma separated
    color = db.Column(db.String(20), default="#2563eb")
    active = db.Column(db.Boolean, default=True)
    notes = db.Column(db.String(255), default="")

    reservations = db.relationship("Reservation", backref="space", lazy=True)

    def activities_list(self):
        return [a.strip() for a in self.activities.split(",") if a.strip()]


class Ministry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    active = db.Column(db.Boolean, default=True)

    reservations = db.relationship("Reservation", backref="ministry", lazy=True)


class Reservation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    space_id = db.Column(db.Integer, db.ForeignKey("space.id"), nullable=False)
    ministry_id = db.Column(db.Integer, db.ForeignKey("ministry.id"), nullable=True)

    requester_name = db.Column(db.String(120), nullable=False)
    requester_contact = db.Column(db.String(120), default="")
    activity = db.Column(db.String(120), default="")

    date = db.Column(db.String(10), nullable=False)  # YYYY-MM-DD
    start_time = db.Column(db.String(5), nullable=False)  # HH:MM
    end_time = db.Column(db.String(5), nullable=False)  # HH:MM

    is_paid = db.Column(db.Boolean, default=False)
    payment_method = db.Column(db.String(50), default="")
    payment_value = db.Column(db.Float, default=0.0)
    payment_proof_filename = db.Column(db.String(255), default="")
    payment_confirmed = db.Column(db.Boolean, default=False)

    status = db.Column(db.String(20), default="Confirmada")
    notes = db.Column(db.Text, default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.String(120), default="")

    recurring_booking_id = db.Column(db.Integer, db.ForeignKey("recurring_booking.id"), nullable=True)

    def datetime_range_str(self):
        return f"{self.date} {self.start_time} - {self.end_time}"


class RecurringBooking(db.Model):
    __tablename__ = "recurring_booking"

    id = db.Column(db.Integer, primary_key=True)
    space_id = db.Column(db.Integer, db.ForeignKey("space.id"), nullable=False)
    ministry_id = db.Column(db.Integer, db.ForeignKey("ministry.id"), nullable=True)

    requester_name = db.Column(db.String(120), nullable=False)
    requester_contact = db.Column(db.String(120), default="")
    activity = db.Column(db.String(120), default="")

    weekdays = db.Column(db.String(20), nullable=False)  # ex: "0,2,4" (Segunda=0 ... Domingo=6)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5), nullable=False)
    start_date = db.Column(db.String(10), nullable=False)  # primeira data considerada

    billing_type = db.Column(db.String(20), default="Gratuita")  # Gratuita | Mensal
    monthly_value = db.Column(db.Float, default=0.0)
    payment_method = db.Column(db.String(50), default="")

    status = db.Column(db.String(20), default="Ativa")  # Ativa | Encerrada
    notes = db.Column(db.Text, default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.String(120), default="")

    space = db.relationship("Space", backref="recurring_bookings")
    ministry = db.relationship("Ministry", backref="recurring_bookings")
    reservations = db.relationship(
        "Reservation", backref="recurring_booking", lazy=True,
        order_by="Reservation.date"
    )
    bills = db.relationship(
        "MonthlyBill", backref="recurring_booking", lazy=True,
        order_by="MonthlyBill.month", cascade="all, delete-orphan"
    )

    def weekdays_list(self):
        return [int(x) for x in self.weekdays.split(",") if x != ""]

    def weekdays_labels(self):
        labels = dict(DIAS_SEMANA)
        return [labels[str(d)] for d in self.weekdays_list()]


class MonthlyBill(db.Model):
    __tablename__ = "monthly_bill"

    id = db.Column(db.Integer, primary_key=True)
    recurring_booking_id = db.Column(db.Integer, db.ForeignKey("recurring_booking.id"), nullable=False)

    month = db.Column(db.String(7), nullable=False)  # "YYYY-MM"
    value = db.Column(db.Float, default=0.0)
    payment_method = db.Column(db.String(50), default="")
    payment_confirmed = db.Column(db.Boolean, default=False)
    payment_proof_filename = db.Column(db.String(255), default="")
    paid_at = db.Column(db.String(10), default="")
    notes = db.Column(db.Text, default="")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def seed_data():
    """Seed initial admin user, spaces and sample ministries if DB is empty."""
    if not User.query.first():
        admin = User(name="Gestor", email="gestor@ong.org")
        admin.set_password("mudar123")
        db.session.add(admin)

    if not Space.query.first():
        # Cores alinhadas à identidade visual (Pátio Esportes): tons de verde e neutros.
        spaces = [
            Space(name="Quadra de Cimento", space_type="Quadra", activities="Vôlei, Futebol, Basquete", color="#3f846a"),
            Space(name="Quadra de Areia", space_type="Quadra", activities="Tênis, Vôlei, Futevôlei", color="#b9975b"),
            Space(name="Sala 1", space_type="Sala", activities="Reunião, Estudo, Curso", color="#6b8f87"),
            Space(name="Churrasqueira", space_type="Churrasco", activities="Confraternização", color="#2b2f2e"),
        ]
        db.session.add_all(spaces)

    if not Ministry.query.first():
        ministries = [Ministry(name=n) for n in [
            "Louvor", "Jovens", "Crianças", "Casais", "Ação Social", "Evangelismo"
        ]]
        db.session.add_all(ministries)

    db.session.commit()
