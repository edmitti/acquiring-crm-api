
import os
from uuid import UUID, uuid4
from typing import Literal
from pathlib import Path
from fastapi.responses import FileResponse

import psycopg
from psycopg.types.json import Jsonb
from psycopg.rows import dict_row
from dotenv import load_dotenv

from fastapi import FastAPI, Header, HTTPException, UploadFile, File
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


# Загружаем настройки из .env рядом с main.py
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = FastAPI(title="Acquiring CRM API")

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}

# Настройки подключения к PostgreSQL
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "acquiring_crm"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
}



def get_connection():
    if not DB_CONFIG["password"]:
        raise RuntimeError(
            "Не задан DB_PASSWORD в файле .env"
        )

    return psycopg.connect(
        **DB_CONFIG,
        row_factory=dict_row,
        connect_timeout=5,
    )

# Модель входящих данных заявки
class ApplicationIn(BaseModel):
    fullName: str = Field(min_length=3)
    phone: str = Field(pattern=r"^\+7[0-9]{10}$")
    inn: str = Field(pattern=r"^[0-9]{10}([0-9]{2})?$")
    source: Literal["SITE", "APP"]
    businessType: str | None = None
    monthlyTurnover: float | None = None
    averageCheck: float | None = None
    city: str | None = None




@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    return JSONResponse(
        status_code=400,
        content={
            "code": "VALIDATION_ERROR",
            "message": "Проверьте входные данные",
        },
    )


@app.get("/health")
def health_check():
    """Проверка доступности API и подключения к БД."""
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")

        return {
            "status": "ok",
            "database": "connected",
        }

    except Exception:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DATABASE_UNAVAILABLE",
                "message": "База данных временно недоступна",
            },
        )


@app.post("/applications", status_code=201)
def create_application(
    body: ApplicationIn,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=1,
    ),
):
    request_data = body.model_dump()

    # Готовим идентификатор новой заявки.
    new_application_id = uuid4()

    # Пытаемся создать запись. Уникальное ограничение БД
    # защищает от одновременного создания по одному ключу.
    with get_connection() as conn:
        inserted = conn.execute(
            """
            INSERT INTO applications (
                application_id,
                idempotency_key,
                request_body,
                status,
                inn_verification_status,
                client_exists,
                client_id
            )
            VALUES (%s, %s, %s, 'NEW', 'PENDING', FALSE, NULL)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING
                application_id,
                status,
                inn_verification_status,
                client_exists,
                client_id
            """,
            (
                new_application_id,
                idempotency_key,
                Jsonb(request_data),
            ),
        ).fetchone()

        if inserted is not None:
            record = inserted
        else:
            # Такой ключ уже существует. Сравниваем данные.
            record = conn.execute(
                """
                SELECT
                    application_id,
                    request_body,
                    status,
                    inn_verification_status,
                    client_exists,
                    client_id
                FROM applications
                WHERE idempotency_key = %s
                """,
                (idempotency_key,),
            ).fetchone()

            if record is None:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "DATABASE_CONFLICT",
                        "message": "Повторите запрос позже",
                    },
                )

            if record["request_body"] != request_data:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "IDEMPOTENCY_KEY_REUSED",
                        "message": (
                            "Ключ Idempotency-Key использован "
                            "с другими данными"
                        ),
                    },
                )

    # Возвращаем привычный формат ответа API.
    return {
        "applicationId": str(record["application_id"]),
        "status": record["status"],
        "innVerificationStatus": record["inn_verification_status"],
        "clientExists": record["client_exists"],
        "clientId": (
            str(record["client_id"])
            if record["client_id"] is not None
            else None
        ),
    }




@app.post("/attachments", status_code=201)
async def upload_attachment(file: UploadFile = File(...)):
    original_name = Path(file.filename or "").name
    extension = Path(original_name).suffix.lower()

    if not original_name:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "FILE_REQUIRED",
                "message": "Файл обязателен",
            },
        )

    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "UNSUPPORTED_FILE_TYPE",
                "message": "Разрешены только PDF, PNG и JPEG",
            },
        )

    attachment_id = str(uuid4())
    safe_name = f"{attachment_id}{extension}"
    destination = UPLOAD_DIR / safe_name
    size = 0
    file_header = b""

    try:
        with destination.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                # Сохраняем начало файла для проверки сигнатуры.
                if len(file_header) < 8:
                    needed = 8 - len(file_header)
                    file_header += chunk[:needed]

                size += len(chunk)

                if size > MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "code": "FILE_TOO_LARGE",
                            "message": "Максимальный размер файла — 10 МиБ",
                        },
                    )

                output.write(chunk)

    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "FILE_SAVE_ERROR",
                "message": "Не удалось сохранить файл",
            },
        )
    finally:
        await file.close()

    if size == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "EMPTY_FILE",
                "message": "Файл пуст",
            },
        )

    # Проверяем сигнатуру содержимого, а не только расширение.
    valid_signatures = {
        ".pdf": lambda header: header.startswith(b"%PDF-"),
        ".png": lambda header: header.startswith(
            b"\x89PNG\r\n\x1a\n"
        ),
        ".jpg": lambda header: header.startswith(b"\xff\xd8\xff"),
        ".jpeg": lambda header: header.startswith(b"\xff\xd8\xff"),
    }

    is_valid_content = valid_signatures[extension](file_header)

    if not is_valid_content:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_FILE_CONTENT",
                "message": "Содержимое файла не соответствует его расширению",
            },
        )

    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO attachments (
                    attachment_id,
                    filename,
                    file_path,
                    content_type,
                    file_size
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    attachment_id,
                    original_name,
                    str(destination.resolve()),
                    file.content_type,
                    size,
                ),
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ATTACHMENT_METADATA_SAVE_ERROR",
                "message": "Не удалось сохранить данные вложения",
            },
        )

    return {
        "attachmentId": attachment_id,
        "fileName": original_name,
        "size": size,
    }


@app.get("/attachments/{attachment_id}")
def get_attachment(attachment_id: UUID):
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    attachment_id,
                    filename,
                    content_type,
                    file_size,
                    created_at
                FROM attachments
                WHERE attachment_id = %s
                """,
                (attachment_id,),
            ).fetchone()

    except Exception:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ATTACHMENT_METADATA_READ_ERROR",
                "message": "Не удалось получить данные вложения",
            },
        )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ATTACHMENT_NOT_FOUND",
                "message": "Вложение не найдено",
            },
        )

    return {
        "attachmentId": str(row["attachment_id"]),
        "fileName": row["filename"],
        "contentType": row["content_type"],
        "size": row["file_size"],
        "createdAt": row["created_at"].isoformat(),
    }
@app.get("/attachments/{attachment_id}/download")
def download_attachment(attachment_id: UUID):
    try:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT filename, file_path
                FROM attachments
                WHERE attachment_id = %s
                """,
                (attachment_id,),
            ).fetchone()

    except Exception:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "ATTACHMENT_METADATA_READ_ERROR",
                "message": "Не удалось получить данные вложения",
            },
        )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ATTACHMENT_NOT_FOUND",
                "message": "Вложение не найдено",
            },
        )

    file_path = Path(row["file_path"])

    if not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ATTACHMENT_FILE_NOT_FOUND",
                "message": "Файл вложения не найден на диске",
            },
        )

    return FileResponse(
        path=file_path,
        filename=row["filename"],
        media_type="application/octet-stream",
    )