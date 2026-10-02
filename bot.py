from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, quote, urlencode

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - requirements installs it
    load_dotenv = lambda: None


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("roulette_bot")


# -----------------------------
# Configuration
# -----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not CHANNEL_ID_RAW:
    raise RuntimeError("CHANNEL_ID is required")
if not CHANNEL_URL:
    raise RuntimeError("CHANNEL_URL is required")


def parse_admin_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError as exc:
                raise RuntimeError(f"Invalid ADMIN_IDS value: {part!r}") from exc
    return result


ADMIN_IDS = parse_admin_ids(ADMIN_IDS_RAW)


def parse_chat_id(raw: str) -> int | str:
    try:
        return int(raw)
    except ValueError:
        return raw


CHANNEL_ID = parse_chat_id(CHANNEL_ID_RAW)

MIN_WITHDRAWAL = Decimal("50.00")
CARD_RE = re.compile(r"^\d{13,19}$")

DEFAULT_PRIZES = [
    (Decimal("10.00"), Decimal("55.0000")),
    (Decimal("20.00"), Decimal("30.0000")),
    (Decimal("30.00"), Decimal("5.0000")),
    (Decimal("50.00"), Decimal("0.0000")),
    (Decimal("100.00"), Decimal("0.0000")),
    (Decimal("200.00"), Decimal("0.0000")),
    (Decimal("300.00"), Decimal("0.0000")),
    (Decimal("500.00"), Decimal("0.0000")),
    (Decimal("1000.00"), Decimal("0.0000")),
]


# -----------------------------
# Database
# -----------------------------


def normalize_database_url(url: str) -> str:
    if not url:
        return "sqlite+aiosqlite:///./bot.db"
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgresql+asyncpg://") or url.startswith("sqlite+"):
        return url
    if url.startswith("sqlite:///"):
        return "sqlite+aiosqlite:///" + url[len("sqlite:///") :]
    return url


DB_URL = normalize_database_url(DATABASE_URL)

engine_kwargs: dict[str, Any] = {"echo": False, "future": True}
if DB_URL.startswith("postgresql+"):
    engine_kwargs["pool_pre_ping"] = True
    engine_kwargs["pool_size"] = 5
    engine_kwargs["max_overflow"] = 10

engine = create_async_engine(DB_URL, **engine_kwargs)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.utcnow()


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("telegram_id", name="uq_users_telegram_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(255), nullable=False, default="Користувач")
    referrer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    referral_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    referral_spins_awarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    total_won: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    total_withdrawn: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    welcome_spin_given: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_subscribed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class ReferralModel(Base):
    __tablename__ = "referrals"
    __table_args__ = (UniqueConstraint("referred_id", name="uq_referrals_referred"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    referred_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class RoulettePrizeModel(Base):
    __tablename__ = "roulette_prizes"
    __table_args__ = (
        CheckConstraint("probability >= 0 AND probability <= 100", name="ck_prize_probability"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, unique=True)
    probability: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("0"))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SpinModel(Base):
    __tablename__ = "spins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    prize_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class WithdrawalModel(Base):
    __tablename__ = "withdrawals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    card_number: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class TransactionModel(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AdminLogModel(Base):
    __tablename__ = "admin_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        existing = await session.scalar(select(func.count(RoulettePrizeModel.id)))
        if not existing:
            for amount, probability in DEFAULT_PRIZES:
                session.add(
                    RoulettePrizeModel(
                        amount=amount,
                        probability=probability,
                        active=True,
                    )
                )
            await session.commit()
            logger.info("Default roulette prizes inserted")


# -----------------------------
# Telegram / app helpers
# -----------------------------

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router(name="main")
dp.include_router(router)

BOT_USERNAME = ""


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def money(value: Decimal | float | int | None) -> str:
    if value is None:
        return "0.00"
    return f"{Decimal(str(value)):.2f}"


def display_name(user: UserModel) -> str:
    return user.first_name or user.username or str(user.telegram_id)


def masked_card(card: str) -> str:
    return f"**** **** **** {card[-4:]}"


def html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_dt(value: datetime | None) -> str:
    if not value:
        return "—"
    return value.strftime("%d.%m.%Y %H:%M")


def normalize_amount(raw: str) -> Decimal:
    value = Decimal(raw.strip().replace(",", "."))
    value = value.quantize(Decimal("0.01"))
    if value <= 0:
        raise InvalidOperation
    return value


async def get_user(telegram_id: int, session: AsyncSession | None = None) -> UserModel | None:
    own = session is None
    if own:
        session = SessionLocal()
    try:
        return await session.scalar(select(UserModel).where(UserModel.telegram_id == telegram_id))
    finally:
        if own:
            await session.close()


async def ensure_user(tg_user: Any, referrer_id: int | None = None) -> UserModel:
    async with SessionLocal() as session:
        user = await session.scalar(select(UserModel).where(UserModel.telegram_id == tg_user.id))
        if user:
            changed = False
            username = getattr(tg_user, "username", None)
            first_name = getattr(tg_user, "first_name", None) or "Користувач"
            if user.username != username:
                user.username = username
                changed = True
            if user.first_name != first_name:
                user.first_name = first_name
                changed = True
            if changed:
                user.updated_at = utcnow()
                await session.commit()
            return user

        valid_referrer = None
        if referrer_id and referrer_id != tg_user.id:
            referrer = await session.scalar(select(UserModel).where(UserModel.telegram_id == referrer_id))
            if referrer and not referrer.is_blocked:
                valid_referrer = referrer_id

        user = UserModel(
            telegram_id=tg_user.id,
            username=getattr(tg_user, "username", None),
            first_name=getattr(tg_user, "first_name", None) or "Користувач",
            referrer_id=valid_referrer,
            referral_count=0,
            referral_spins_awarded=0,
            spins=0,
            balance=Decimal("0.00"),
            total_won=Decimal("0.00"),
            total_withdrawn=Decimal("0.00"),
            welcome_spin_given=False,
            is_subscribed=False,
            is_active=True,
            is_blocked=False,
        )
        session.add(user)
        await session.commit()
        return user


async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }
    except TelegramBadRequest as exc:
        logger.warning("Subscription check failed for %s: %s", user_id, exc)
        return False
    except TelegramNetworkError:
        logger.exception("Telegram network error during subscription check")
        raise


async def confirm_subscription_and_rewards(user_id: int) -> tuple[bool, bool, int]:
    async with SessionLocal() as session:
        user = await session.scalar(select(UserModel).where(UserModel.telegram_id == user_id))
        if not user:
            return False, False, 0

        subscribed = await check_subscription(user_id)
        if not subscribed:
            user.is_subscribed = False
            user.updated_at = utcnow()
            await session.commit()
            return False, False, 0

        welcome_granted = False
        if not user.welcome_spin_given:
            user.welcome_spin_given = True
            user.spins += 1
            welcome_granted = True
            session.add(
                TransactionModel(
                    user_id=user.id,
                    type="welcome_spin",
                    amount=Decimal("1.00"),
                    description="Вітальний безкоштовний спін",
                )
            )

        referral_reward_count = 0
        if user.referrer_id:
            existing_ref = await session.scalar(
                select(ReferralModel).where(ReferralModel.referred_id == user.telegram_id)
            )
            if not existing_ref:
                referrer = await session.scalar(
                    select(UserModel).where(UserModel.telegram_id == user.referrer_id)
                )
                if referrer and not referrer.is_blocked and referrer.telegram_id != user.telegram_id:
                    session.add(
                        ReferralModel(
                            referrer_id=referrer.telegram_id,
                            referred_id=user.telegram_id,
                        )
                    )
                    referrer.referral_count += 1
                    eligible_spins = referrer.referral_count // 5
                    if eligible_spins > referrer.referral_spins_awarded:
                        delta = eligible_spins - referrer.referral_spins_awarded
                        referrer.spins += delta
                        referrer.referral_spins_awarded = eligible_spins
                        referral_reward_count = delta
                        session.add(
                            TransactionModel(
                                user_id=referrer.id,
                                type="referral_spin",
                                amount=Decimal(delta),
                                description=f"Нагорода за {delta * 5} підтверджених рефералів",
                            )
                        )

        user.is_subscribed = True
        user.is_active = True
        user.updated_at = utcnow()
        await session.commit()
        return True, welcome_granted, referral_reward_count


async def update_user_activity(user_id: int, active: bool = True) -> None:
    async with SessionLocal() as session:
        user = await session.scalar(select(UserModel).where(UserModel.telegram_id == user_id))
        if user:
            user.is_active = active
            user.updated_at = utcnow()
            await session.commit()


async def get_referral_link(user_id: int) -> str:
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username or ""
    return f"https://t.me/{BOT_USERNAME}?start={user_id}"


async def build_main_menu(user_id: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="👤 Мій кабінет", callback_data="menu:cabinet")],
        [InlineKeyboardButton(text="🎡 Крутити колесо", callback_data="menu:roulette")],
        [InlineKeyboardButton(text="👥 Запросити друзів", callback_data="menu:referrals"), InlineKeyboardButton(text="💰 Вивід коштів", callback_data="menu:withdraw")],
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="menu:stats"), InlineKeyboardButton(text="📜 Правила", callback_data="menu:rules")],
        [InlineKeyboardButton(text="ℹ️ Допомога", callback_data="menu:help")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="⚙️ Адмін-панель", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu:home")]]
    )


def subscription_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Підписатися на канал", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Я підписався", callback_data="check:subscription")],
        ]
    )


def roulette_markup(user_id: int) -> InlineKeyboardMarkup:
    if not WEBAPP_URL:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⚠️ Web App ще не налаштовано", callback_data="menu:home")],
                [InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu:home")],
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎡 Відкрити рулетку", web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton(text="👥 Запросити друзів", callback_data="menu:referrals")],
            [InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu:home")],
        ]
    )


def share_url_markup(link: str) -> InlineKeyboardMarkup:
    share_text = "🎁 Запрошую тебе в бонусну рулетку! Заходь та отримуй безкоштовний спін."
    share_url = f"https://t.me/share/url?{urlencode({'url': link, 'text': share_text})}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поділитися посиланням", url=share_url)],
            [InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu:home")],
        ]
    )


async def safe_edit(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        if callback.message:
            await callback.message.edit_text(text, reply_markup=reply_markup)
        await callback.answer()
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            await callback.answer()
        else:
            logger.warning("Failed to edit message: %s", exc)
            await callback.answer("Не вдалося оновити повідомлення.", show_alert=True)


async def send_main_menu(target: Message | CallbackQuery, user: UserModel | None = None) -> None:
    if user is None:
        user = await get_user(target.from_user.id)
    if not user:
        return
    text = "🎉 <b>ГОЛОВНЕ МЕНЮ</b>\n\nОбирай потрібну дію нижче 👇"
    markup = await build_main_menu(user.telegram_id)
    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


# -----------------------------
# Texts / screens
# -----------------------------


def subscription_text() -> str:
    return (
        "👋 <b>Вітаємо!</b>\n\n"
        "🎁 Ти отримав можливість взяти участь у нашій бонусній системі.\n\n"
        "Щоб відкрити доступ до бота:\n\n"
        "1️⃣ Підпишись на наш Telegram-канал\n"
        "2️⃣ Натисни <b>«✅ Я підписався»</b>"
    )


def rules_text() -> str:
    return (
        "📜 <b>ПРАВИЛА</b>\n\n"
        "1️⃣ Кожен новий користувач після підтвердження підписки отримує 1 безкоштовний спін.\n\n"
        "2️⃣ За кожних 5 підтверджених рефералів нараховується 1 додатковий спін.\n\n"
        "3️⃣ Один Telegram-акаунт може бути врахований як реферал лише один раз.\n\n"
        "4️⃣ Заборонено використовувати ботів, фейкові акаунти та інші способи накрутки.\n\n"
        "5️⃣ Результат рулетки визначається сервером відповідно до поточних налаштувань шансів.\n\n"
        "6️⃣ Виграш автоматично додається на баланс.\n\n"
        f"7️⃣ Мінімальна сума виводу — <b>{money(MIN_WITHDRAWAL)} грн</b>.\n\n"
        "8️⃣ Заявки на виплату перевіряються адміністратором.\n\n"
        "9️⃣ У випадку порушення правил адміністрація може скасувати підозрілу активність.\n\n"
        "🔟 Усі операції фіксуються в системі."
    )


def help_text() -> str:
    return (
        "❓ <b>ЯК ЦЕ ПРАЦЮЄ?</b>\n\n"
        "1. Підпишись на канал.\n"
        "2. Отримай безкоштовний спін.\n"
        "3. Крути рулетку.\n"
        "4. Отримуй виграш на баланс.\n"
        "5. Запрошуй друзів.\n"
        "6. За кожні 5 підтверджених рефералів отримуй новий спін.\n"
        "7. Виводь кошти після досягнення мінімальної суми."
    )


async def cabinet_text(user: UserModel) -> str:
    progress = user.referral_count % 5
    return (
        "👤 <b>МОЄ КАБІНЕТ</b>\n\n"
        f"👤 Ім'я: <b>{html_escape(display_name(user))}</b>\n"
        f"🆔 Telegram ID: <code>{user.telegram_id}</code>\n\n"
        f"👥 Запрошено друзів: <b>{user.referral_count}</b>\n"
        f"🎡 Доступних спінів: <b>{user.spins}</b>\n\n"
        f"📊 Прогрес до наступного спіну: <b>{progress}/5</b>\n\n"
        f"💰 Поточний баланс: <b>{money(user.balance)} грн</b>\n"
        f"🏆 Всього виграно: <b>{money(user.total_won)} грн</b>"
    )


async def referrals_text(user: UserModel) -> str:
    link = await get_referral_link(user.telegram_id)
    progress = user.referral_count % 5
    return (
        "👥 <b>ЗАПРОШУЙ ДРУЗІВ</b>\n\n"
        "Запрошуй друзів та отримуй безкоштовні прокрути рулетки.\n\n"
        "🎁 Кожні 5 підтверджених рефералів = <b>+1 спін</b>.\n\n"
        f"👥 Запрошено: <b>{user.referral_count}</b>\n"
        f"🎡 До наступного спіну: <b>{progress}/5</b>\n"
        f"🎁 Доступних спінів: <b>{user.spins}</b>\n\n"
        f"🔗 <b>Твоє реферальне посилання:</b>\n<code>{link}</code>"
    )


def referrals_markup(user_id: int, link: str) -> InlineKeyboardMarkup:
    share_text = "🎁 Запрошую тебе в бонусну рулетку! Заходь та отримуй безкоштовний спін."
    share = f"https://t.me/share/url?{urlencode({'url': link, 'text': share_text})}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поділитися посиланням", url=share)],
            [InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu:home")],
        ]
    )


async def stats_text(user: UserModel) -> str:
    async with SessionLocal() as session:
        spin_count = await session.scalar(
            select(func.count(SpinModel.id)).where(SpinModel.user_id == user.id)
        )
        payouts = await session.execute(
            select(WithdrawalModel)
            .where(WithdrawalModel.user_id == user.id)
            .order_by(WithdrawalModel.created_at.desc())
            .limit(10)
        )
        withdrawals = list(payouts.scalars())
    lines = [
        "📊 <b>МОЯ СТАТИСТИКА</b>",
        "",
        f"🎡 Всього прокрутів: <b>{spin_count or 0}</b>",
        f"🏆 Всього виграно: <b>{money(user.total_won)} грн</b>",
        f"💰 Поточний баланс: <b>{money(user.balance)} грн</b>",
        f"👥 Підтверджених рефералів: <b>{user.referral_count}</b>",
        "",
        "💳 <b>Історія виплат:</b>",
    ]
    if not withdrawals:
        lines.append("— Поки немає заявок.")
    else:
        status_map = {"pending": "⏳ очікує", "paid": "✅ виплачено", "rejected": "❌ відхилено"}
        for item in withdrawals:
            lines.append(f"{status_map.get(item.status, item.status)} — {money(item.amount)} грн")
    return "\n".join(lines)


# -----------------------------
# FSM states
# -----------------------------


class WithdrawalStates(StatesGroup):
    waiting_card = State()
    waiting_amount = State()


class AdminSpinStates(StatesGroup):
    waiting_user_id = State()
    waiting_delta = State()


class AdminBalanceStates(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()
    waiting_reason = State()


class AdminPrizeStates(StatesGroup):
    waiting_probability = State()


class AdminBroadcastStates(StatesGroup):
    waiting_text = State()
    waiting_confirm = State()


class AdminBlockStates(StatesGroup):
    waiting_user_id = State()


# -----------------------------
# User handlers
# -----------------------------


@router.message(CommandStart())
async def start_handler(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    referrer_id: int | None = None
    if command.args:
        try:
            candidate = int(command.args.strip())
            if candidate != message.from_user.id:
                referrer_id = candidate
        except ValueError:
            referrer_id = None

    user = await ensure_user(message.from_user, referrer_id=referrer_id)
    if user.is_blocked:
        await message.answer("🚫 <b>Доступ до бота заблоковано.</b>\n\nЗверніться до адміністрації.")
        return
    await update_user_activity(user.telegram_id, active=True)

    if user.is_subscribed and user.welcome_spin_given:
        user = await get_user(user.telegram_id)
        await send_main_menu(message, user)
        return

    await message.answer(subscription_text(), reply_markup=subscription_markup())


@router.callback_query(F.data == "check:subscription")
async def subscription_check_handler(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user:
        user = await ensure_user(callback.from_user)
    if user.is_blocked:
        await callback.answer("Доступ заблоковано.", show_alert=True)
        return

    try:
        subscribed, welcome_granted, referral_reward_count = await confirm_subscription_and_rewards(
            callback.from_user.id
        )
    except TelegramNetworkError:
        await callback.answer("Telegram тимчасово недоступний. Спробуй ще раз.", show_alert=True)
        return

    if not subscribed:
        await callback.answer("Ти ще не підписаний на канал.", show_alert=True)
        return

    message_text = "🎉 <b>Підписку підтверджено!</b>\n\n"
    if welcome_granted:
        message_text += "Тобі нараховано 🎁 <b>1 БЕЗКОШТОВНИЙ СПІН!</b>\n\n"
    else:
        message_text += "✅ Підписка вже була підтверджена раніше.\n\n"
    if referral_reward_count:
        message_text += f"🎁 Твій запрошувач отримав <b>{referral_reward_count}</b> бонусний спін за нових рефералів.\n\n"
    message_text += "🎡 Тепер доступне головне меню."
    user = await get_user(callback.from_user.id)
    await safe_edit(callback, message_text, await build_main_menu(callback.from_user.id))


@router.callback_query(F.data == "menu:home")
async def menu_home(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Спочатку натисни /start.", show_alert=True)
        return
    if user.is_blocked:
        await callback.answer("Доступ заблоковано.", show_alert=True)
        return
    if not user.is_subscribed:
        await safe_edit(callback, subscription_text(), subscription_markup())
        return
    await send_main_menu(callback, user)


@router.callback_query(F.data == "menu:cabinet")
async def menu_cabinet(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Спочатку натисни /start.", show_alert=True)
        return
    await safe_edit(
        callback,
        await cabinet_text(user),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Історія виплат", callback_data="menu:payments_history")],
                [InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu:home")],
            ]
        ),
    )


@router.callback_query(F.data == "menu:payments_history")
async def menu_payments_history(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Спочатку натисни /start.", show_alert=True)
        return
    async with SessionLocal() as session:
        result = await session.execute(
            select(WithdrawalModel)
            .where(WithdrawalModel.user_id == user.id)
            .order_by(WithdrawalModel.created_at.desc())
            .limit(20)
        )
        withdrawals = list(result.scalars())
    status_map = {"pending": "⏳ очікує", "paid": "✅ виплачено", "rejected": "❌ відхилено"}
    lines = ["💳 <b>ІСТОРІЯ ВИПЛАТ</b>", ""]
    if not withdrawals:
        lines.append("— Поки немає виплат.")
    else:
        for item in withdrawals:
            lines.append(
                f"#{item.id} — {money(item.amount)} грн — {status_map.get(item.status, item.status)} — {format_dt(item.created_at)}"
            )
    await safe_edit(
        callback,
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="menu:cabinet")]],
        ),
    )


@router.callback_query(F.data == "menu:roulette")
async def menu_roulette(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Спочатку натисни /start.", show_alert=True)
        return
    if user.spins <= 0:
        await safe_edit(
            callback,
            "❌ <b>У тебе немає доступних прокрутів.</b>\n\n"
            "Запроси ще 5 друзів, щоб отримати наступний безкоштовний спін.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="👥 Запросити друзів", callback_data="menu:referrals")],
                    [InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu:home")],
                ]
            ),
        )
        return
    await safe_edit(
        callback,
        "🎡 <b>РУЛЕТКА</b>\n\n"
        f"Доступних спінів: <b>{user.spins}</b>\n\n"
        "Натисни кнопку нижче, щоб відкрити красиву анімовану рулетку.",
        roulette_markup(user.telegram_id),
    )


@router.callback_query(F.data == "menu:referrals")
async def menu_referrals(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Спочатку натисни /start.", show_alert=True)
        return
    link = await get_referral_link(user.telegram_id)
    await safe_edit(callback, await referrals_text(user), referrals_markup(user.telegram_id, link))


@router.callback_query(F.data == "menu:stats")
async def menu_stats(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Спочатку натисни /start.", show_alert=True)
        return
    await safe_edit(
        callback,
        await stats_text(user),
        back_menu_markup(),
    )


@router.callback_query(F.data == "menu:rules")
async def menu_rules(callback: CallbackQuery) -> None:
    await safe_edit(callback, rules_text(), back_menu_markup())


@router.callback_query(F.data == "menu:help")
async def menu_help(callback: CallbackQuery) -> None:
    await safe_edit(callback, help_text(), back_menu_markup())


# -----------------------------
# Withdrawals
# -----------------------------


def withdraw_intro(user: UserModel) -> str:
    return (
        "💰 <b>ВИВІД КОШТІВ</b>\n\n"
        f"Твій баланс: <b>{money(user.balance)} грн</b>\n\n"
        f"Мінімальна сума для виводу: <b>{money(MIN_WITHDRAWAL)} грн</b>\n"
        "Максимальна сума: весь доступний баланс\n\n"
        "Натисни кнопку нижче, щоб створити заявку."
    )


@router.callback_query(F.data == "menu:withdraw")
async def menu_withdraw(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Спочатку натисни /start.", show_alert=True)
        return
    await safe_edit(
        callback,
        withdraw_intro(user),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💸 Створити заявку", callback_data="withdraw:create")],
                [InlineKeyboardButton(text="💳 Історія виплат", callback_data="menu:payments_history")],
                [InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu:home")],
            ]
        ),
    )


@router.callback_query(F.data == "withdraw:create")
async def withdraw_create(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.answer("Спочатку натисни /start.", show_alert=True)
        return
    if user.balance < MIN_WITHDRAWAL:
        await callback.answer(
            f"Мінімальна сума для виводу — {money(MIN_WITHDRAWAL)} грн.",
            show_alert=True,
        )
        return
    await state.set_state(WithdrawalStates.waiting_card)
    await callback.message.answer("💳 <b>Введи номер банківської картки:</b>\n\nЛише цифри, 13–19 символів.")
    await callback.answer()


@router.message(WithdrawalStates.waiting_card)
async def withdrawal_card_input(message: Message, state: FSMContext) -> None:
    card = (message.text or "").strip().replace(" ", "").replace("-", "")
    if not CARD_RE.fullmatch(card):
        await message.answer("❌ Номер картки має містити 13–19 цифр. Спробуй ще раз.")
        return
    await state.update_data(card_number=card)
    await state.set_state(WithdrawalStates.waiting_amount)
    user = await get_user(message.from_user.id)
    if not user:
        await state.clear()
        return
    await message.answer(
        f"💰 <b>Введи суму для виводу:</b>\n\n"
        f"Мінімум: {money(MIN_WITHDRAWAL)} грн\n"
        f"Доступно: {money(user.balance)} грн"
    )


@router.message(WithdrawalStates.waiting_amount)
async def withdrawal_amount_input(message: Message, state: FSMContext) -> None:
    try:
        amount = normalize_amount(message.text or "")
    except (InvalidOperation, ValueError):
        await message.answer("❌ Введи коректну суму, наприклад: 100 або 100.50")
        return

    data = await state.get_data()
    card_number = str(data.get("card_number", ""))
    if not card_number:
        await state.clear()
        await message.answer("⚠️ Сесію скасовано. Почни створення заявки ще раз.")
        return

    async with SessionLocal() as session:
        user = await session.scalar(select(UserModel).where(UserModel.telegram_id == message.from_user.id))
        if not user:
            await state.clear()
            await message.answer("⚠️ Користувача не знайдено. Натисни /start.")
            return
        if user.is_blocked:
            await state.clear()
            await message.answer("🚫 Доступ заблоковано.")
            return
        if amount < MIN_WITHDRAWAL:
            await message.answer(f"❌ Мінімальна сума для виводу — {money(MIN_WITHDRAWAL)} грн.")
            return
        if amount > user.balance:
            await message.answer(f"❌ Недостатньо коштів. Доступно: {money(user.balance)} грн.")
            return

        duplicate = await session.scalar(
            select(WithdrawalModel).where(
                WithdrawalModel.user_id == user.id,
                WithdrawalModel.amount == amount,
                WithdrawalModel.status == "pending",
            )
        )
        if duplicate:
            await message.answer("❌ У тебе вже є активна заявка на цю суму.")
            return

        user.balance -= amount
        user.updated_at = utcnow()
        withdrawal = WithdrawalModel(
            user_id=user.id,
            amount=amount,
            card_number=card_number,
            status="pending",
        )
        session.add(withdrawal)
        await session.flush()
        session.add(
            TransactionModel(
                user_id=user.id,
                type="withdraw_pending",
                amount=-amount,
                description=f"Резервування коштів для заявки #{withdrawal.id}",
            )
        )
        await session.commit()
        withdrawal_id = withdrawal.id

    await state.clear()
    await message.answer(
        "✅ <b>Заявку на вивід створено!</b>\n\n"
        f"💰 Сума: <b>{money(amount)} грн</b>\n"
        f"💳 Карта: <b>{masked_card(card_number)}</b>\n"
        "Статус: <b>⏳ Очікує виплати</b>\n\n"
        f"ID заявки: <code>#{withdrawal_id}</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Історія виплат", callback_data="menu:payments_history")],
                [InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu:home")],
            ]
        ),
    )


# -----------------------------
# Admin helpers and menus
# -----------------------------


def admin_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👥 Користувачі", callback_data="admin:users")],
            [InlineKeyboardButton(text="💰 Баланси", callback_data="admin:balance")],
            [InlineKeyboardButton(text="🎡 Налаштування рулетки", callback_data="admin:roulette")],
            [InlineKeyboardButton(text="🎁 Видати спіни", callback_data="admin:spins")],
            [InlineKeyboardButton(text="💸 Виплати", callback_data="admin:withdrawals")],
            [InlineKeyboardButton(text="📢 Розсилка", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="📜 Історія виграшів", callback_data="admin:spins_history")],
            [InlineKeyboardButton(text="🔧 Налаштування", callback_data="admin:settings")],
            [InlineKeyboardButton(text="🚫 Заблоковані користувачі", callback_data="admin:blocked")],
            [InlineKeyboardButton(text="🏠 Головне меню", callback_data="menu:home")],
        ]
    )


async def admin_guard(callback: CallbackQuery) -> bool:
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ заборонено.", show_alert=True)
        return False
    return True


@router.callback_query(F.data == "admin:menu")
async def admin_menu(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await safe_edit(callback, "⚙️ <b>АДМІН-ПАНЕЛЬ</b>\n\nОберіть потрібний розділ:", admin_menu_markup())


@router.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    async with SessionLocal() as session:
        total = await session.scalar(select(func.count(UserModel.id))) or 0
        active = await session.scalar(
            select(func.count(UserModel.id)).where(UserModel.is_active.is_(True), UserModel.is_blocked.is_(False))
        ) or 0
        result = await session.execute(select(UserModel).order_by(UserModel.created_at.desc()).limit(20))
        users = list(result.scalars())
    lines = [
        "👥 <b>КОРИСТУВАЧІ</b>",
        "",
        f"Всього: <b>{total}</b> | Активних: <b>{active}</b>",
        "",
    ]
    for user in users:
        uname = f"@{html_escape(user.username)}" if user.username else "без username"
        blocked = " 🚫" if user.is_blocked else ""
        lines.append(f"<code>{user.telegram_id}</code> — {html_escape(user.first_name)} — {uname} — {money(user.balance)} грн{blocked}")
    await safe_edit(
        callback,
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔧 Керування користувачем", callback_data="admin:user_tools")],
                [InlineKeyboardButton(text="◀️ Адмін-панель", callback_data="admin:menu")],
            ]
        ),
    )


class AdminUserToolsStates(StatesGroup):
    waiting_user_id = State()


@router.callback_query(F.data == "admin:user_tools")
async def admin_user_tools(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.set_state(AdminUserToolsStates.waiting_user_id)
    await callback.message.answer("🆔 Введи Telegram ID користувача для перегляду/блокування:")
    await callback.answer()


@router.message(AdminUserToolsStates.waiting_user_id)
async def admin_user_tools_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Telegram ID має бути числом.")
        return
    async with SessionLocal() as session:
        user = await session.scalar(select(UserModel).where(UserModel.telegram_id == user_id))
    if not user:
        await message.answer("❌ Користувача не знайдено.")
        return
    await state.clear()
    status = "🚫 заблокований" if user.is_blocked else "✅ активний"
    await message.answer(
        f"👤 <b>Користувач</b> <code>{user.telegram_id}</code>\n\n"
        f"Ім'я: {html_escape(user.first_name)}\n"
        f"Username: @{html_escape(user.username or '—')}\n"
        f"Баланс: <b>{money(user.balance)} грн</b>\n"
        f"Спіни: <b>{user.spins}</b>\n"
        f"Реферали: <b>{user.referral_count}</b>\n"
        f"Статус: {status}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Заблокувати" if not user.is_blocked else "✅ Розблокувати", callback_data=f"admin:toggle_block:{user.telegram_id}")],
                [InlineKeyboardButton(text="◀️ Користувачі", callback_data="admin:users")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("admin:toggle_block:"))
async def admin_toggle_block(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    try:
        user_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Некоректний ID.", show_alert=True)
        return
    async with SessionLocal() as session:
        user = await session.scalar(select(UserModel).where(UserModel.telegram_id == user_id))
        if not user:
            await callback.answer("Користувача не знайдено.", show_alert=True)
            return
        user.is_blocked = not user.is_blocked
        user.updated_at = utcnow()
        action = "block_user" if user.is_blocked else "unblock_user"
        session.add(
            AdminLogModel(
                admin_id=callback.from_user.id,
                action=action,
                target_user_id=user_id,
                amount=None,
                description=f"Користувач {'заблокований' if user.is_blocked else 'розблокований'}",
            )
        )
        await session.commit()
        new_status = user.is_blocked
    await safe_edit(
        callback,
        f"✅ Статус користувача <code>{user_id}</code> змінено: {'🚫 заблокований' if new_status else '✅ активний'}",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ Користувачі", callback_data="admin:users")]],
        ),
    )


@router.callback_query(F.data == "admin:balance")
async def admin_balance(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.set_state(AdminBalanceStates.waiting_user_id)
    await callback.message.answer("💰 Введи Telegram ID користувача:")
    await callback.answer()


@router.message(AdminBalanceStates.waiting_user_id)
async def admin_balance_user_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        target_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Telegram ID має бути числом.")
        return
    user = await get_user(target_id)
    if not user:
        await message.answer("❌ Користувача не знайдено.")
        return
    await state.update_data(target_user_id=target_id)
    await state.set_state(AdminBalanceStates.waiting_amount)
    await message.answer(
        f"✅ Користувач знайдений. Поточний баланс: <b>{money(user.balance)} грн</b>\n\n"
        "Введи суму з плюсом або мінусом. Наприклад: <code>100</code> або <code>-50</code>."
    )


@router.message(AdminBalanceStates.waiting_amount)
async def admin_balance_amount_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation:
        await message.answer("❌ Некоректна сума.")
        return
    if amount == 0:
        await message.answer("❌ Сума не може бути 0.")
        return
    await state.update_data(amount=str(amount))
    await state.set_state(AdminBalanceStates.waiting_reason)
    await message.answer("📝 Введи причину зміни балансу:")


@router.message(AdminBalanceStates.waiting_reason)
async def admin_balance_reason_input(message: Message, state: FSMContext) -> None:
    reason = (message.text or "").strip()
    data = await state.get_data()
    try:
        target_id = int(data["target_user_id"])
        amount = Decimal(data["amount"]).quantize(Decimal("0.01"))
    except (KeyError, ValueError, InvalidOperation):
        await state.clear()
        await message.answer("⚠️ Сесію скасовано. Почни заново.")
        return
    if not reason:
        await message.answer("❌ Вкажи причину.")
        return
    async with SessionLocal() as session:
        user = await session.scalar(select(UserModel).where(UserModel.telegram_id == target_id))
        if not user:
            await state.clear()
            await message.answer("❌ Користувача не знайдено.")
            return
        if amount < 0 and user.balance + amount < 0:
            await message.answer("❌ Баланс не може стати від'ємним.")
            return
        user.balance += amount
        user.updated_at = utcnow()
        session.add(
            TransactionModel(
                user_id=user.id,
                type="admin_balance_add" if amount > 0 else "admin_balance_remove",
                amount=amount,
                description=reason,
            )
        )
        session.add(
            AdminLogModel(
                admin_id=message.from_user.id,
                action="adjust_balance",
                target_user_id=target_id,
                amount=amount,
                description=reason,
            )
        )
        await session.commit()
        new_balance = user.balance
    await state.clear()
    await message.answer(
        f"✅ Баланс користувача <code>{target_id}</code> змінено на <b>{money(amount)} грн</b>.\n"
        f"💰 Новий баланс: <b>{money(new_balance)} грн</b>"
    )
    try:
        await bot.send_message(
            target_id,
            f"ℹ️ Адміністрація змінила твій баланс на <b>{money(amount)} грн</b>.\nПричина: {html_escape(reason)}\n\n"
            f"💰 Новий баланс: <b>{money(new_balance)} грн</b>",
        )
    except Exception:
        logger.exception("Failed to notify user %s about balance adjustment", target_id)


async def roulette_prizes_text() -> str:
    async with SessionLocal() as session:
        result = await session.execute(select(RoulettePrizeModel).order_by(RoulettePrizeModel.id))
        prizes = list(result.scalars())
    total = sum((Decimal(str(p.probability)) for p in prizes if p.active), Decimal("0"))
    lines = ["🎡 <b>НАЛАШТУВАННЯ РУЛЕТКИ</b>", ""]
    for prize in prizes:
        lines.append(f"{money(prize.amount)} грн — <b>{Decimal(str(prize.probability)):.4f}%</b>")
    lines.append("")
    lines.append(f"Сума активних шансів: <b>{total:.4f}%</b>")
    return "\n".join(lines)


def roulette_admin_markup(prizes: Iterable[RoulettePrizeModel]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for prize in prizes:
        rows.append([
            InlineKeyboardButton(
                text=f"✏️ Змінити {money(prize.amount)} грн",
                callback_data=f"admin:prize:{prize.id}",
            )
        ])
    rows.append([InlineKeyboardButton(text="◀️ Адмін-панель", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin:roulette")
async def admin_roulette(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    async with SessionLocal() as session:
        result = await session.execute(select(RoulettePrizeModel).order_by(RoulettePrizeModel.id))
        prizes = list(result.scalars())
    await safe_edit(callback, await roulette_prizes_text(), roulette_admin_markup(prizes))


def prize_draft_markup(prizes: list[RoulettePrizeModel]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for prize in prizes:
        rows.append([
            InlineKeyboardButton(
                text=f"✏️ Змінити {money(prize.amount)} грн",
                callback_data=f"admin:prize-draft:{prize.id}",
            )
        ])
    rows.append([InlineKeyboardButton(text="💾 Зберегти зміни", callback_data="admin:prize-save")])
    rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="admin:prize-cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_prize_draft_text(draft: dict[str, str] | None = None) -> str:
    draft = draft or {}
    async with SessionLocal() as session:
        result = await session.execute(select(RoulettePrizeModel).order_by(RoulettePrizeModel.id))
        prizes = list(result.scalars())
    total = Decimal("0")
    lines = ["🎡 <b>ЧЕРНЕТКА НАЛАШТУВАНЬ РУЛЕТКИ</b>", ""]
    for prize in prizes:
        value = Decimal(draft.get(str(prize.id), str(prize.probability)))
        total += value if prize.active else Decimal("0")
        lines.append(f"{money(prize.amount)} грн — <b>{value:.4f}%</b>")
    lines.append("")
    status = "✅ Можна зберігати" if total == Decimal("100.0000") else "⚠️ Потрібно довести суму до 100%"
    lines.append(f"Сума: <b>{total:.4f}%</b> — {status}")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("admin:prize:"))
async def admin_prize_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    try:
        prize_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Некоректний ID.", show_alert=True)
        return
    async with SessionLocal() as session:
        result = await session.execute(select(RoulettePrizeModel).order_by(RoulettePrizeModel.id))
        prizes = list(result.scalars())
        prize = next((p for p in prizes if p.id == prize_id), None)
    if not prize:
        await callback.answer("Приз не знайдено.", show_alert=True)
        return
    draft = {str(p.id): f"{Decimal(str(p.probability)):.4f}" for p in prizes}
    await state.update_data(draft=draft, editing_prize_id=prize_id)
    await state.set_state(AdminPrizeStates.waiting_probability)
    await callback.message.answer(
        f"✏️ Поточний шанс для <b>{money(prize.amount)} грн</b>: <b>{Decimal(str(prize.probability)):.4f}%</b>\n\n"
        "Введи новий процент від 0 до 100.\n\n"
        "Можна змінити кілька призів у чернетці, а потім натиснути «💾 Зберегти зміни»."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:prize-draft:"))
async def admin_prize_draft_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    try:
        prize_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Некоректний ID.", show_alert=True)
        return
    data = await state.get_data()
    draft = data.get("draft")
    if not isinstance(draft, dict):
        async with SessionLocal() as session:
            result = await session.execute(select(RoulettePrizeModel).order_by(RoulettePrizeModel.id))
            prizes = list(result.scalars())
        draft = {str(p.id): f"{Decimal(str(p.probability)):.4f}" for p in prizes}
    await state.update_data(draft=draft, editing_prize_id=prize_id)
    await state.set_state(AdminPrizeStates.waiting_probability)
    async with SessionLocal() as session:
        prize = await session.scalar(select(RoulettePrizeModel).where(RoulettePrizeModel.id == prize_id))
    if not prize:
        await callback.answer("Приз не знайдено.", show_alert=True)
        return
    await callback.message.answer(
        f"✏️ Новий шанс для <b>{money(prize.amount)} грн</b>\n\n"
        "Введи число від 0 до 100, наприклад <code>12.5000</code>."
    )
    await callback.answer()


@router.message(AdminPrizeStates.waiting_probability)
async def admin_prize_probability_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        value = Decimal((message.text or "").strip().replace(",", ".")).quantize(Decimal("0.0001"))
    except InvalidOperation:
        await message.answer("❌ Введи коректний процент.")
        return
    if value < 0 or value > 100:
        await message.answer("❌ Процент повинен бути від 0 до 100.")
        return
    data = await state.get_data()
    prize_id = int(data.get("editing_prize_id", 0))
    draft = data.get("draft")
    if not isinstance(draft, dict):
        await state.clear()
        await message.answer("⚠️ Чернетка втрачена. Відкрий налаштування рулетки ще раз.")
        return
    draft[str(prize_id)] = f"{value:.4f}"
    await state.update_data(draft=draft)

    async with SessionLocal() as session:
        result = await session.execute(select(RoulettePrizeModel).order_by(RoulettePrizeModel.id))
        prizes = list(result.scalars())
    total = sum(
        (Decimal(draft.get(str(p.id), str(p.probability))) for p in prizes if p.active),
        Decimal("0"),
    )
    await message.answer(await build_prize_draft_text(draft), reply_markup=prize_draft_markup(prizes))
    if total != Decimal("100.0000"):
        await message.answer(
            "❌ <b>Поточна чернетка ще не готова до збереження.</b>\n\n"
            f"Сума шансів зараз: <b>{total:.4f}%</b>.\n"
            "Зміни інші призи або поверни попереднє значення, після чого натисни «💾 Зберегти зміни»."
        )


@router.callback_query(F.data == "admin:prize-save")
async def admin_prize_save(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    data = await state.get_data()
    draft = data.get("draft")
    if not isinstance(draft, dict):
        await callback.answer("Немає активної чернетки.", show_alert=True)
        return
    async with SessionLocal() as session:
        result = await session.execute(select(RoulettePrizeModel).order_by(RoulettePrizeModel.id))
        prizes = list(result.scalars())
        parsed: dict[int, Decimal] = {}
        for prize in prizes:
            raw = draft.get(str(prize.id), str(prize.probability))
            try:
                parsed[prize.id] = Decimal(raw).quantize(Decimal("0.0001"))
            except InvalidOperation:
                await callback.answer("Є некоректний шанс.", show_alert=True)
                return
        total = sum((parsed[p.id] for p in prizes if p.active), Decimal("0"))
        if total != Decimal("100.0000"):
            await callback.answer(
                f"❌ Сума шансів повинна дорівнювати 100%. Зараз {total:.4f}%.",
                show_alert=True,
            )
            return
        for prize in prizes:
            old = Decimal(str(prize.probability))
            new = parsed[prize.id]
            if old != new:
                prize.probability = new
                session.add(
                    AdminLogModel(
                        admin_id=callback.from_user.id,
                        action="update_prize_probability",
                        target_user_id=None,
                        amount=prize.amount,
                        description=f"Приз {money(prize.amount)}: {old:.4f}% -> {new:.4f}%",
                    )
                )
        await session.commit()
    await state.clear()
    await safe_edit(
        callback,
        await roulette_prizes_text(),
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Адмін-панель", callback_data="admin:menu")]]),
    )


@router.callback_query(F.data == "admin:prize-cancel")
async def admin_prize_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.clear()
    await safe_edit(
        callback,
        await roulette_prizes_text(),
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Адмін-панель", callback_data="admin:menu")]]),
    )


@router.callback_query(F.data == "admin:spins")
async def admin_spins(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.set_state(AdminSpinStates.waiting_user_id)
    await callback.message.answer("🎁 Введи Telegram ID користувача:")
    await callback.answer()


@router.message(AdminSpinStates.waiting_user_id)
async def admin_spins_user_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        user_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Telegram ID має бути числом.")
        return
    user = await get_user(user_id)
    if not user:
        await message.answer("❌ Користувача не знайдено.")
        return
    await state.update_data(target_user_id=user_id)
    await state.set_state(AdminSpinStates.waiting_delta)
    await message.answer(
        f"🎁 Поточні спіни: <b>{user.spins}</b>\n\n"
        "Введи кількість спінів з плюсом або мінусом. Наприклад: <code>5</code> або <code>-5</code>."
    )


@router.message(AdminSpinStates.waiting_delta)
async def admin_spins_delta_input(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        delta = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Введи ціле число, наприклад 5 або -5.")
        return
    if delta == 0:
        await message.answer("❌ Кількість не може бути 0.")
        return
    data = await state.get_data()
    target_id = int(data.get("target_user_id", 0))
    async with SessionLocal() as session:
        user = await session.scalar(select(UserModel).where(UserModel.telegram_id == target_id))
        if not user:
            await state.clear()
            await message.answer("❌ Користувача не знайдено.")
            return
        if user.spins + delta < 0:
            await message.answer("❌ Кількість спінів не може стати від'ємною.")
            return
        user.spins += delta
        user.updated_at = utcnow()
        session.add(
            AdminLogModel(
                admin_id=message.from_user.id,
                action="adjust_spins",
                target_user_id=target_id,
                amount=Decimal(delta),
                description="Ручна зміна кількості спінів",
            )
        )
        await session.commit()
        spins_now = user.spins
    await state.clear()
    await message.answer(
        f"✅ Користувачу <code>{target_id}</code> {'додано' if delta > 0 else 'знято'} <b>{abs(delta)}</b> спінів.\n"
        f"🎁 Поточні спіни: <b>{spins_now}</b>"
    )


async def admin_statistics_text() -> str:
    now = utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    async with SessionLocal() as session:
        total_users = await session.scalar(select(func.count(UserModel.id))) or 0
        active_users = await session.scalar(
            select(func.count(UserModel.id)).where(UserModel.is_active.is_(True), UserModel.is_blocked.is_(False))
        ) or 0
        new_day = await session.scalar(
            select(func.count(UserModel.id)).where(UserModel.created_at >= day_ago)
        ) or 0
        new_week = await session.scalar(
            select(func.count(UserModel.id)).where(UserModel.created_at >= week_ago)
        ) or 0
        total_refs = await session.scalar(select(func.count(ReferralModel.id))) or 0
        total_spins = await session.scalar(select(func.count(SpinModel.id))) or 0
        total_won = await session.scalar(select(func.coalesce(func.sum(SpinModel.prize_amount), 0))) or 0
        total_paid = await session.scalar(
            select(func.coalesce(func.sum(WithdrawalModel.amount), 0)).where(WithdrawalModel.status == "paid")
        ) or 0
        pending = await session.scalar(
            select(func.coalesce(func.sum(WithdrawalModel.amount), 0)).where(WithdrawalModel.status == "pending")
        ) or 0
    return (
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"👥 Всього користувачів: <b>{total_users}</b>\n"
        f"🟢 Активних: <b>{active_users}</b>\n"
        f"📅 Нових сьогодні: <b>{new_day}</b>\n"
        f"📅 Нових за тиждень: <b>{new_week}</b>\n\n"
        f"👥 Всього рефералів: <b>{total_refs}</b>\n"
        f"🎡 Всього прокрутів: <b>{total_spins}</b>\n"
        f"💰 Всього виграно: <b>{money(total_won)} грн</b>\n"
        f"💸 Всього виплачено: <b>{money(total_paid)} грн</b>\n"
        f"⏳ Очікує виплати: <b>{money(pending)} грн</b>"
    )


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await safe_edit(
        callback,
        await admin_statistics_text(),
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Адмін-панель", callback_data="admin:menu")]]),
    )


@router.callback_query(F.data == "admin:spins_history")
async def admin_spins_history(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    async with SessionLocal() as session:
        result = await session.execute(
            select(SpinModel, UserModel)
            .join(UserModel, SpinModel.user_id == UserModel.id)
            .order_by(SpinModel.created_at.desc())
            .limit(30)
        )
        rows = list(result.all())
    lines = ["📜 <b>ІСТОРІЯ ВИГРАШІВ</b>", ""]
    if not rows:
        lines.append("— Поки немає прокрутів.")
    else:
        for spin, user in rows:
            lines.append(f"#{spin.id} — <code>{user.telegram_id}</code> — +{money(spin.prize_amount)} грн — {format_dt(spin.created_at)}")
    await safe_edit(
        callback,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Адмін-панель", callback_data="admin:menu")]]),
    )


@router.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    text = (
        "🔧 <b>НАЛАШТУВАННЯ</b>\n\n"
        f"📢 Канал: {html_escape(CHANNEL_URL)}\n"
        f"💰 Мінімальна виплата: <b>{money(MIN_WITHDRAWAL)} грн</b>\n"
        f"🌐 Web App: <b>{'налаштований' if WEBAPP_URL else 'не налаштований'}</b>\n\n"
        "Налаштування з ENV: BOT_TOKEN, ADMIN_IDS, CHANNEL_ID, CHANNEL_URL, DATABASE_URL, WEBAPP_URL."
    )
    await safe_edit(
        callback,
        text,
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Адмін-панель", callback_data="admin:menu")]]),
    )


@router.callback_query(F.data == "admin:blocked")
async def admin_blocked(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    async with SessionLocal() as session:
        result = await session.execute(
            select(UserModel).where(UserModel.is_blocked.is_(True)).order_by(UserModel.updated_at.desc()).limit(50)
        )
        users = list(result.scalars())
    lines = ["🚫 <b>ЗАБЛОКОВАНІ КОРИСТУВАЧІ</b>", ""]
    if not users:
        lines.append("— Немає заблокованих користувачів.")
    else:
        for user in users:
            lines.append(f"<code>{user.telegram_id}</code> — {html_escape(user.first_name)} — {format_dt(user.updated_at)}")
    await safe_edit(
        callback,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Адмін-панель", callback_data="admin:menu")]]),
    )


# -----------------------------
# Admin withdrawals
# -----------------------------


@router.callback_query(F.data == "admin:withdrawals")
async def admin_withdrawals(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await show_pending_withdrawals(callback)


async def show_pending_withdrawals(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(WithdrawalModel, UserModel)
            .join(UserModel, WithdrawalModel.user_id == UserModel.id)
            .where(WithdrawalModel.status == "pending")
            .order_by(WithdrawalModel.created_at.asc())
            .limit(20)
        )
        rows = list(result.all())
    lines = ["💸 <b>ВИПЛАТИ</b>", ""]
    if not rows:
        lines.append("— Немає заявок, що очікують.")
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Адмін-панель", callback_data="admin:menu")]])
    else:
        for withdrawal, user in rows:
            uname = f"@{html_escape(user.username)}" if user.username else "без username"
            lines.append(
                f"<b>#{withdrawal.id}</b> — <code>{user.telegram_id}</code> — {uname}\n"
                f"💰 {money(withdrawal.amount)} грн | 💳 {masked_card(withdrawal.card_number)} | {format_dt(withdrawal.created_at)}\n"
            )
        rows_buttons: list[list[InlineKeyboardButton]] = []
        for withdrawal, _ in rows:
            rows_buttons.append([
                InlineKeyboardButton(text=f"✅ #{withdrawal.id}", callback_data=f"admin:withdraw:pay:{withdrawal.id}"),
                InlineKeyboardButton(text=f"❌ #{withdrawal.id}", callback_data=f"admin:withdraw:reject:{withdrawal.id}"),
            ])
        rows_buttons.append([InlineKeyboardButton(text="◀️ Адмін-панель", callback_data="admin:menu")])
        markup = InlineKeyboardMarkup(inline_keyboard=rows_buttons)
    await safe_edit(callback, "\n".join(lines), markup)


@router.callback_query(F.data.startswith("admin:withdraw:pay:"))
async def admin_withdraw_pay(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    try:
        withdrawal_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Некоректний ID.", show_alert=True)
        return
    async with SessionLocal() as session:
        withdrawal = await session.scalar(select(WithdrawalModel).where(WithdrawalModel.id == withdrawal_id))
        if not withdrawal:
            await callback.answer("Заявку не знайдено.", show_alert=True)
            return
        if withdrawal.status != "pending":
            await callback.answer("Заявка вже оброблена.", show_alert=True)
            return
        user = await session.scalar(select(UserModel).where(UserModel.id == withdrawal.user_id))
        withdrawal.status = "paid"
        withdrawal.processed_at = utcnow()
        withdrawal.admin_id = callback.from_user.id
        if user:
            user.total_withdrawn += withdrawal.amount
            user.updated_at = utcnow()
            session.add(
                TransactionModel(
                    user_id=user.id,
                    type="withdraw_paid",
                    amount=withdrawal.amount,
                    description=f"Виплата підтверджена. Заявка #{withdrawal.id}",
                )
            )
        session.add(
            AdminLogModel(
                admin_id=callback.from_user.id,
                action="withdraw_paid",
                target_user_id=user.telegram_id if user else None,
                amount=withdrawal.amount,
                description=f"Підтверджено заявку #{withdrawal.id}",
            )
        )
        await session.commit()
        target_id = user.telegram_id if user else None
        amount = withdrawal.amount
    if target_id:
        try:
            await bot.send_message(
                target_id,
                f"✅ <b>Виплату підтверджено!</b>\n\n💰 Сума: <b>{money(amount)} грн</b>\nСтатус: <b>✅ Виплачено</b>",
            )
        except Exception:
            logger.exception("Failed to notify user about paid withdrawal")
    await show_pending_withdrawals(callback)


@router.callback_query(F.data.startswith("admin:withdraw:reject:"))
async def admin_withdraw_reject(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    try:
        withdrawal_id = int(callback.data.split(":")[-1])
    except ValueError:
        await callback.answer("Некоректний ID.", show_alert=True)
        return
    async with SessionLocal() as session:
        withdrawal = await session.scalar(select(WithdrawalModel).where(WithdrawalModel.id == withdrawal_id))
        if not withdrawal:
            await callback.answer("Заявку не знайдено.", show_alert=True)
            return
        if withdrawal.status != "pending":
            await callback.answer("Заявка вже оброблена.", show_alert=True)
            return
        user = await session.scalar(select(UserModel).where(UserModel.id == withdrawal.user_id))
        withdrawal.status = "rejected"
        withdrawal.processed_at = utcnow()
        withdrawal.admin_id = callback.from_user.id
        if user:
            user.balance += withdrawal.amount
            user.updated_at = utcnow()
            session.add(
                TransactionModel(
                    user_id=user.id,
                    type="withdraw_refund",
                    amount=withdrawal.amount,
                    description=f"Повернення коштів після відхилення заявки #{withdrawal.id}",
                )
            )
        session.add(
            AdminLogModel(
                admin_id=callback.from_user.id,
                action="withdraw_rejected",
                target_user_id=user.telegram_id if user else None,
                amount=withdrawal.amount,
                description=f"Відхилено заявку #{withdrawal.id}",
            )
        )
        await session.commit()
        target_id = user.telegram_id if user else None
        amount = withdrawal.amount
    if target_id:
        try:
            await bot.send_message(
                target_id,
                f"❌ <b>Заявку на вивід відхилено.</b>\n\n💰 {money(amount)} грн повернуто на баланс.",
            )
        except Exception:
            logger.exception("Failed to notify user about rejected withdrawal")
    await show_pending_withdrawals(callback)


# -----------------------------
# Admin broadcast
# -----------------------------


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.set_state(AdminBroadcastStates.waiting_text)
    await callback.message.answer("📢 <b>Введи текст розсилки:</b>")
    await callback.answer()


@router.message(AdminBroadcastStates.waiting_text)
async def admin_broadcast_text(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Текст не може бути порожнім.")
        return
    await state.update_data(broadcast_text=text)
    await state.set_state(AdminBroadcastStates.waiting_confirm)
    await message.answer(
        "📢 <b>Текст розсилки:</b>\n\n"
        f"{html_escape(text)}\n\n"
        "⚠️ <b>Підтвердити розсилку?</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Почати розсилку", callback_data="admin:broadcast:confirm")],
                [InlineKeyboardButton(text="❌ Скасувати", callback_data="admin:broadcast:cancel")],
            ]
        ),
    )


@router.callback_query(F.data == "admin:broadcast:cancel")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.clear()
    await callback.message.answer("❌ Розсилку скасовано.")
    await callback.answer()


async def do_broadcast(admin_id: int, text: str) -> tuple[int, int]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(UserModel.telegram_id).where(
                UserModel.is_active.is_(True),
                UserModel.is_blocked.is_(False),
            )
        )
        user_ids = [row[0] for row in result.all()]

    success = 0
    failed = 0
    for telegram_id in user_ids:
        try:
            await bot.send_message(telegram_id, text, parse_mode=None)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.5)
            try:
                await bot.send_message(telegram_id, text, parse_mode=None)
                success += 1
            except TelegramForbiddenError:
                failed += 1
                await update_user_activity(telegram_id, active=False)
            except Exception:
                failed += 1
                logger.exception("Broadcast retry failed for %s", telegram_id)
        except TelegramForbiddenError:
            failed += 1
            await update_user_activity(telegram_id, active=False)
        except TelegramBadRequest as exc:
            failed += 1
            logger.warning("Broadcast bad request to %s: %s", telegram_id, exc)
        except Exception:
            failed += 1
            logger.exception("Broadcast failed to %s", telegram_id)
    async with SessionLocal() as session:
        session.add(
            AdminLogModel(
                admin_id=admin_id,
                action="broadcast",
                target_user_id=None,
                amount=None,
                description=f"Розсилка завершена: успішно {success}, помилок {failed}",
            )
        )
        await session.commit()
    return success, failed


@router.callback_query(F.data == "admin:broadcast:confirm")
async def admin_broadcast_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await state.clear()
        await callback.answer("Немає тексту розсилки.", show_alert=True)
        return
    await state.clear()
    await callback.message.answer("📢 Розсилку запущено. Після завершення надішлю статистику.")
    await callback.answer()
    success, failed = await do_broadcast(callback.from_user.id, str(text))
    try:
        await callback.message.answer(
            "📢 <b>Розсилку завершено.</b>\n\n"
            f"✅ Успішно: <b>{success}</b>\n"
            f"❌ Не доставлено: <b>{failed}</b>"
        )
    except TelegramForbiddenError:
        pass


# -----------------------------
# Commands / fallback
# -----------------------------


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ заборонено.")
        return
    await message.answer("⚙️ <b>АДМІН-ПАНЕЛЬ</b>", reply_markup=admin_menu_markup())


@router.message()
async def fallback_message(message: Message) -> None:
    user = await get_user(message.from_user.id)
    if user and user.is_blocked:
        await message.answer("🚫 Доступ до бота заблоковано.")
        return
    await message.answer("Використай /start або кнопки меню 👇", reply_markup=await build_main_menu(message.from_user.id))


@router.error()
async def global_error_handler(event: ErrorEvent) -> None:
    logger.exception("Unhandled update error", exc_info=event.exception)
    update = event.update
    if update.callback_query:
        try:
            await update.callback_query.answer("⚠️ Сталася помилка. Спробуй ще раз.", show_alert=True)
        except Exception:
            pass
    elif update.message:
        try:
            await update.message.answer("⚠️ Сталася технічна помилка. Спробуй ще раз.")
        except Exception:
            pass


# -----------------------------
# Web App secure API
# -----------------------------


app = FastAPI(title="Referral Roulette Web App", version="1.0.0")


class SpinRequest(BaseModel):
    init_data: str = Field(..., min_length=10, max_length=8192)


class HealthResponse(BaseModel):
    status: str


def validate_webapp_init_data(init_data: str, max_age_seconds: int = 3600) -> dict[str, Any]:
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash")
    auth_date_raw = pairs.get("auth_date")
    if not auth_date_raw:
        raise HTTPException(status_code=401, detail="Missing auth_date")
    try:
        auth_date = int(auth_date_raw)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid auth_date") from exc
    if int(time.time()) - auth_date > max_age_seconds:
        raise HTTPException(status_code=401, detail="Expired Telegram initData")
    if auth_date - int(time.time()) > 60:
        raise HTTPException(status_code=401, detail="Invalid auth_date")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram initData signature")

    user_raw = pairs.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail="Missing Telegram user")
    try:
        tg_user = json.loads(user_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram user payload") from exc
    if not isinstance(tg_user, dict) or not tg_user.get("id"):
        raise HTTPException(status_code=401, detail="Invalid Telegram user")
    return {"user": tg_user, "auth_date": auth_date}


async def choose_prize(session: AsyncSession) -> RoulettePrizeModel:
    result = await session.execute(
        select(RoulettePrizeModel)
        .where(RoulettePrizeModel.active.is_(True), RoulettePrizeModel.probability > 0)
        .order_by(RoulettePrizeModel.id)
    )
    prizes = list(result.scalars())
    if not prizes:
        raise HTTPException(status_code=503, detail="Roulette is not configured")
    total = sum((Decimal(str(p.probability)) for p in prizes), Decimal("0"))
    if total != Decimal("100.0000"):
        raise HTTPException(status_code=503, detail="Roulette probabilities must total 100%")

    scale = Decimal("10000")
    total_units = int(total * scale)
    target = secrets.randbelow(total_units)
    cumulative = 0
    for prize in prizes:
        units = int(Decimal(str(prize.probability)) * scale)
        cumulative += units
        if target < cumulative:
            return prize
    return prizes[-1]


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    try:
        async with SessionLocal() as session:
            await session.execute(select(func.count(UserModel.id)))
        return HealthResponse(status="ok")
    except Exception as exc:
        logger.exception("Health check failed")
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(exc)})


@app.get("/")
async def web_root() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


@app.get("/webapp")
async def webapp_root() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


@app.get("/web/style.css")
async def web_style() -> FileResponse:
    return FileResponse(WEB_DIR / "style.css", media_type="text/css")


@app.get("/web/app.js")
async def web_script() -> FileResponse:
    return FileResponse(WEB_DIR / "app.js", media_type="application/javascript")


@app.get("/api/prizes")
async def api_prizes() -> dict[str, Any]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(RoulettePrizeModel).where(RoulettePrizeModel.active.is_(True)).order_by(RoulettePrizeModel.id)
        )
        prizes = list(result.scalars())
    return {
        "prizes": [
            {
                "id": prize.id,
                "amount": float(prize.amount),
                "label": money(prize.amount),
            }
            for prize in prizes
        ]
    }


@app.post("/api/spin")
async def api_spin(payload: SpinRequest) -> dict[str, Any]:
    validated = validate_webapp_init_data(payload.init_data)
    telegram_user = validated["user"]
    telegram_id = int(telegram_user["id"])

    async with SessionLocal() as session:
        # Atomic decrement is the protection against double-click/replay of one spin.
        result = await session.execute(
            update(UserModel)
            .where(
                UserModel.telegram_id == telegram_id,
                UserModel.spins > 0,
                UserModel.is_blocked.is_(False),
                UserModel.is_active.is_(True),
            )
            .values(spins=UserModel.spins - 1, updated_at=utcnow())
        )
        if result.rowcount != 1:
            user = await session.scalar(select(UserModel).where(UserModel.telegram_id == telegram_id))
            if not user:
                raise HTTPException(status_code=403, detail="User not found")
            if user.is_blocked:
                raise HTTPException(status_code=403, detail="User blocked")
            if not user.is_subscribed:
                raise HTTPException(status_code=403, detail="Channel subscription required")
            raise HTTPException(status_code=409, detail="No spins available")

        user = await session.scalar(select(UserModel).where(UserModel.telegram_id == telegram_id))
        if not user:
            raise HTTPException(status_code=500, detail="User state unavailable")
        if not user.is_subscribed:
            await session.rollback()
            raise HTTPException(status_code=403, detail="Channel subscription required")

        prize = await choose_prize(session)
        amount = Decimal(str(prize.amount))
        user.balance += amount
        user.total_won += amount
        user.updated_at = utcnow()
        session.add(
            SpinModel(
                user_id=user.id,
                prize_amount=amount,
            )
        )
        session.add(
            TransactionModel(
                user_id=user.id,
                type="spin_win",
                amount=amount,
                description=f"Виграш рулетки: {money(amount)} грн",
            )
        )
        await session.commit()
        return {
            "success": True,
            "prize_id": prize.id,
            "amount": float(amount),
            "label": money(amount),
            "balance": float(user.balance),
            "spins_left": user.spins,
        }


# -----------------------------
# Startup / shutdown
# -----------------------------


async def configure_bot() -> None:
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    logger.info("Bot started as @%s", BOT_USERNAME)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запустити бота"),
            BotCommand(command="admin", description="Адмін-панель"),
        ]
    )


async def run_web() -> None:
    import uvicorn

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    await init_db()
    await configure_bot()
    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        run_web(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
