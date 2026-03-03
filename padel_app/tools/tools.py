import csv
import os
from datetime import datetime, date, timezone
from dateutil import parser

from flask import current_app, url_for, request
from sqlalchemy.ext.hybrid import hybrid_method, hybrid_property


def dict_to_table(data):
    if not data:
        return None

    headers = list(data[0].keys())
    rows = [list(entry.values()) for entry in data]

    column_widths = [
        max(len(str(row[i])) for row in rows + [headers]) for i in range(len(headers))
    ]
    table = []

    header_row = (
        "| "
        + " | ".join(f"{headers[i]:<{column_widths[i]}}" for i in range(len(headers)))
        + " |"
    )
    table.append(header_row)
    separator_row = "|-" + "-|-".join("-" * width for width in column_widths) + "-|"
    table.append(separator_row)

    for row in rows:
        data_row = (
            "| "
            + " | ".join(f"{row[i]:<{column_widths[i]}}" for i in range(len(headers)))
            + " |"
        )
        table.append(data_row)

    return "\n".join(table)


def is_float(value):
    try:
        float(value)
        return True
    except ValueError:
        return False


def create_csv_for_model(model):
    model_name = model.__name__.lower()
    instances = model.query.all()

    filename = f"data/csv/{model_name}.csv"
    file_path = current_app.root_path + url_for("static", filename=filename)
    instances = [instance.get_dict() for instance in instances]
    if instances:
        fieldnames = instances[0].keys()

        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for row in instances:
                writer.writerow(row)

    return url_for("static", filename=filename)


def upload_csv_to_model(model):
    model_name = model.__name__.lower()
    filename = f"data/csv/{model_name}.csv"
    file_path = current_app.root_path + url_for("static", filename=filename)

    hybrid_properties = get_hybrid_properties(model)

    with open(file_path, mode="r") as csvfile:
        reader = csv.DictReader(csvfile)

        for row in reader:
            filtered_data = {
                k: try_convert(v) for k, v in row.items() if k not in hybrid_properties
            }
            if filtered_data.get("name"):
                existing_instance = model.query.filter_by(
                    name=filtered_data.get("name")
                ).first()
            else:
                existing_instance = model.query.filter_by(
                    id=filtered_data.get("id")
                ).first()

            if existing_instance:
                existing_instance.update_with_dict(filtered_data)
                existing_instance.save()
            else:
                empty_instance = model()
                empty_instance.update_with_dict(filtered_data)
                empty_instance.create()

    return True


def get_hybrid_properties(model):
    hybrid_properties = []
    for name, attr in model.__dict__.items():
        if isinstance(attr, (hybrid_property, hybrid_method)):
            hybrid_properties.append(name)
    return hybrid_properties


def try_convert(value):
    if value == "":
        return None
    converters = [int, float, str_to_bool, str_to_date, str_to_datetime]
    for conv in converters:
        try:
            return conv(value)
        except ValueError:
            pass
    return value


def str_to_bool(s):
    if s.lower() == "true":
        return True
    elif s.lower() == "false":
        return False
    else:
        raise ValueError(f"Cannot convert {s} to boolean")


def str_to_date(s):
    if isinstance(s, datetime):
        return s
    if isinstance(s, date):
        return datetime.combine(s, datetime.min.time())

    string_formats = {
        "Date": [
            "%d/%m/%Y",  # 01/01/2023
            "%m/%d/%Y",  # 01/01/2023 US format
            "%Y-%m-%d",  # 2023-01-01 ISO 8601
            "%d-%m-%Y",  # 01-01-2023
            "%d.%m.%Y",  # 01.01.2023
            "%B %d, %Y",  # January 01, 2023
        ],
    }
    for format in string_formats["Date"]:
        try:
            date_obj = datetime.strptime(s, format)
            return date_obj
        except (ValueError, TypeError):
            pass
    raise ValueError(f"Cannot convert {s} to Date")


def str_to_datetime(s):
    if isinstance(s, datetime):
        return s
    if isinstance(s, date):
        return datetime.combine(s, datetime.min.time())

    string_formats = {
        "DateTime": [
            "%d/%m/%Y, %H:%M",  # 01/01/2023, 12:00
            "%Y-%m-%d",  # 2023-01-01 date-only fallback
            "%Y-%m-%d %H:%M:%S",  # 2023-01-01 12:00:00
            "%Y-%m-%dT%H:%M",  # 2023-01-01T12:00 ISO 8601 without seconds
            "%Y-%m-%dT%H:%M:%S",  # 2023-01-01T12:00:00 ISO 8601 with seconds
            "%Y-%m-%dT%H:%M:%S.%f",  # 2023-01-01T12:00:00.000000 ISO 8601 with microseconds
            "%d-%m-%Y %H:%M",  # 01-01-2023 12:00
            "%m/%d/%Y %I:%M %p",  # 01/01/2023 12:00 PM US format with AM/PM
            "%d %B, %Y %H:%M",  # 01 January, 2023 12:00
            "%A, %d %B %Y %H:%M:%S %Z",  # Tuesday, 01 January 2023 12:00:00 UTC
        ],
    }
    for format in string_formats["DateTime"]:
        try:
            date_obj = datetime.strptime(s, format)
            return date_obj
        except (ValueError, TypeError):
            pass
    raise ValueError(f"Cannot convert {s} to DateTime")

def iso_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None

def _parse_range_or_default():
    start = request.args.get("from")
    end = request.args.get("to")

    now = datetime.now(timezone.utc)

    if not start or not end:
        # Default: next 30 days
        range_start = now
        range_end = now.replace(microsecond=0) + (datetime.now(timezone.utc) - datetime.now(timezone.utc))  # no-op
        range_end = now + (datetime.utcnow() - datetime.utcnow())  # no-op fallback
        range_end = now + (datetime.fromtimestamp(0, tz=timezone.utc) - datetime.fromtimestamp(0, tz=timezone.utc))  # no-op
        # simpler:
        from datetime import timedelta
        range_end = now + timedelta(days=30)
        return range_start, range_end

    range_start = parser.isoparse(start).astimezone(timezone.utc)
    range_end = parser.isoparse(end).astimezone(timezone.utc)
    return range_start, range_end


def _date_label(date_str: str) -> str:
    """
    Produces something like 'Mon 2 Mar' in English-ish format.
    NOTE: %a/%b depends on server locale; if that becomes an issue,
    we can send date+time and let the frontend format.
    """
    d = datetime.fromisoformat(date_str).date()
    return f"{d:%a} {d.day} {d:%b}"


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default
    
