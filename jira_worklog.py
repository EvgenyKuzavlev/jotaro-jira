import argparse
import calendar
import getpass
import json
import locale
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import ClassVar
from urllib.parse import urlparse

import holidays
import pytz
import requests
import yaml
from bs4 import BeautifulSoup
from jira import JIRA, JIRAError
from requests.auth import HTTPBasicAuth

tz = pytz.timezone('Europe/Moscow')
locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
logging_level = logging.INFO
logging.basicConfig(
    format='[%(asctime)s] %(levelname)-8s %(message)s',
    level=logging_level,
    datefmt='%m-%d-%Y %I:%M:%S %p')


@dataclass(frozen=True)
class __PreparedWorklog:
    issue: str
    comment: str
    time_spent: str
    day: datetime
    int_time_spent: int

    def is_empty(self):
        return self.int_time_spent == 0


class DayStatus(Enum):
    WEEK_DAY = "0", 28800
    DAY_OFF = "1", 0
    CUT_DAY = "2", 25200

    def is_day_off(self) -> bool:
        return self.value[0] == "1"

    def is_day_cut(self) -> bool:
        return self.value[0] == "2"

    def available_working_time(self) -> int:
        return self.value[1]

    @classmethod
    def text_to_day_status(cls, text: str):
        for member in cls:
            if member.value[0] == text:
                return member

        raise ValueError(f"Значение может быть только '0', '1' или '2'. text = {text}")

    @classmethod
    def bool_to_day_status(cls, alt_day_off_status: bool):
        return DayStatus.DAY_OFF if alt_day_off_status else DayStatus.WEEK_DAY


@dataclass(frozen=False)
class Day:
    date: datetime
    day_status: DayStatus
    fact_time_spent: int
    user_plan_time_spent: ClassVar[int]

    def available_working_time(self) -> int:
        return self.day_status.available_working_time()


def text_to_boolean(text: str) -> bool:
    if text == "1":
        return True
    elif text == "0":
        return False
    else:
        raise ValueError(f"Текст может быть только '0' или '1'. text = {text}")


def get_yes_no_input(prompt: str) -> bool:
    while True:
        response = input(prompt).lower()
        if response in ['yes', 'y']:
            return True
        elif response in ['no', 'n']:
            return False
        else:
            logging.info("Пожалуйста, введите 'YES/Yes/yes/y' или 'NO/No/no/n'.")


def get_or_else(value, default):
    return default if value is None else value


def is_valid_http_url(url):
    try:
        result = urlparse(url)
        return result.scheme in ('http', 'https') and result.netloc != ''
    except Exception:
        return False


def convert_seconds_to_full_time(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60

    time_str = ""
    time_str += f"{hours}h" if hours > 0 else ""
    time_str += " " if time_str != "" else ""
    time_str += f"{minutes}m" if minutes > 0 else ""
    time_str += " " if time_str != "" else ""
    time_str += f"{seconds}s" if seconds > 0 or time_str == "" else ""

    return time_str.lstrip().rstrip()


def convert_full_time_to_seconds(hours: int = 0, minutes: int = 0, seconds: int = 0) -> int:
    return hours * 3600 + minutes * 60 + seconds


def convert_seconds_to_hours(seconds: int) -> int:
    return seconds // 3600


def is_time_over(seconds: int) -> bool:
    return seconds > 28800


def get_localize_datetime(year: int, month: int, day: int) -> datetime:
    return tz.localize(datetime(year, month, day))


def get_first_and_last_days(year: int, month: int, first_day: int, last_day: int) -> (datetime, datetime):
    first_day_of_month = get_localize_datetime(year, month, first_day)

    if last_day is not None:
        try:
            last_day_of_month = get_localize_datetime(year, month, last_day)
        except ValueError as e:
            logging.error(f"Скорее всего, ошибка в параметре --last-day. Проверьте значения. Ошибка: {e}")
            exit(1)
    else:
        last_day_of_month = get_localize_datetime(year, month, calendar.monthrange(year, month)[1])

    if first_day_of_month > last_day_of_month:
        logging.error(
            f"Введен не правильный диапазон дат: с {first_day_of_month.strftime("%d.%m.%Y")} по {last_day_of_month.strftime("%d.%m.%Y")}")
        exit(1)

    return first_day_of_month, last_day_of_month


def connect_to_jira(jira_url: str, login: str, psswrd: str) -> tuple[JIRA, HTTPBasicAuth]:
    try:
        logging.info(f"Авторизуемся в Jira: {jira_url}")
        jira_options = {'server': jira_url}
        jira = JIRA(options=jira_options, basic_auth=(login, psswrd))
        basic_auth_jira = HTTPBasicAuth(login, psswrd)
    except UnicodeEncodeError:
        logging.error("При вводе пароля использована кириллица. Смените раскладку на латиницу и повторите ввод.")
        exit(1)
    except JIRAError:
        logging.error("Проблемы при авторизации. Проверьте корректность логина и пароля, затем повторите ввод.")
        exit(1)

    logging.info('Авторизация успешна')

    return jira, basic_auth_jira


def calculate_day_status(day: datetime, alt_day_off: int) -> DayStatus:
    if alt_day_off:
        return DayStatus.bool_to_day_status(day.weekday() < 5 and day not in holidays.Russia())

    for i in range(3):
        is_day_off_response = requests.get('https://isdayoff.ru/{}?pre=1'.format(day.strftime("%Y%m%d")))

        if is_day_off_response.status_code == 200:
            return DayStatus.text_to_day_status(is_day_off_response.text)
        else:
            logging.error(f"Ошибка вендора при запросе {f'https://isdayoff.ru/{day.strftime("%Y%m%d")}?pre=1'}, пробую еще раз (попытка №{i+1})")
            time.sleep(3)

    logging.error(f"Не удалось определить статус для даты {day.strftime("%d.%m.%Y")}")
    exit(1)


def get_weekdays(first_day: datetime, last_day: datetime, alt_day_off: int) -> list[Day]:
    logging.info(f'Использована временная зона {tz}')
    logging.info(
        f'Вычисляется количество выходных дней за период с {first_day.strftime("%d.%m.%Y")} по {last_day.strftime("%d.%m.%Y")}')

    if alt_day_off:
        logging.warning(
            'При расчете может использоваться локальный механизм определения выходного дня, проверьте заполненный ворклог')
    else:
        logging.info('При расчете выходных будет использован https://isdayoff.ru/')

    current_day = first_day
    weekdays = []
    day_offs = []
    is_log_cut_day = False

    while current_day <= last_day:
        day_status: DayStatus = calculate_day_status(day=current_day, alt_day_off=alt_day_off)

        if not is_log_cut_day and day_status.is_day_cut():
            is_log_cut_day = True

        if not day_status.is_day_off():
            random_hour = random.randint(11, 20)
            random_minute = random.randint(0, 59)
            random_second = random.randint(0, 59)
            weekdays.append(Day(date=current_day.replace(hour=random_hour, minute=random_minute, second=random_second),
                                day_status=day_status, fact_time_spent=0))
        else:
            day_offs.append(current_day)

        current_day += timedelta(days=1)

    logging.info(f'Рабочие дни: {', '.join([day.date.strftime("%A %d.%m") for day in weekdays])}')

    if is_log_cut_day:
        logging.info(
            f'Сокращенные(предпраздничные) дни: {', '.join([day.date.strftime("%A %d.%m") for day in weekdays if day.day_status.is_day_cut()])}')

    if len(day_offs) != 0:
        logging.info(f'Выходные дни: {', '.join([day.strftime("%A %d.%m") for day in day_offs])}')

    return weekdays


def fill_time_spent(days: list[Day], jira: JIRA, basic_auth_jira: HTTPBasicAuth, username: str) -> list[Day]:
    logging.info("Корректируем планируемое списание времени с учетом уже залогированного времени в Jira")
    overtime = dict()
    all_time_spent = get_jira_worklogs(jira=jira, basic_auth=basic_auth_jira, days=days, username=username)

    for day in days:
        day_str = day.date.strftime('%Y-%m-%d')

        if day_str in all_time_spent:
            total_time_spent = all_time_spent[day_str][0]
            time_spent_log = all_time_spent[day_str][1]
        else:
            total_time_spent = 0
            time_spent_log = None

        if is_time_over(total_time_spent):
            overtime[day_str] = total_time_spent

        if total_time_spent >= day.available_working_time():  # если закончилось рабочее время - нечего  тратить
            day.fact_time_spent = 0  # то тратим 0
        elif day.available_working_time() - total_time_spent < Day.user_plan_time_spent:  # пользователь хочет потрать больше чем у нас есть
            day.fact_time_spent = day.available_working_time() - total_time_spent  # забиваем на пользователя и тратим все что осталось
        elif day.available_working_time() - total_time_spent >= Day.user_plan_time_spent:  # пользователь хочет потрать меньше чем у нас есть
            day.fact_time_spent = Day.user_plan_time_spent  # тратим то что хочет пользователь

        if time_spent_log is not None:
            logging.info(
                f"{day.date.strftime("%d.%m")}: доступное время - {convert_seconds_to_full_time(day.fact_time_spent)}, текущий ворклог - {", ".join(time_spent_log)}")
        else:
            logging.info(
                f"{day.date.strftime("%d.%m")}: доступное время - {convert_seconds_to_full_time(day.fact_time_spent)}, текущий ворклог пуст")

    if len(overtime) != 0:
        input(
            f"Внимание! Обнаружено превышение времени в следующий датах: {", ".join([f'{key}:{convert_seconds_to_full_time(value)}' for key, value in overtime.items()])}\n"
            f"Необходимо вручную проверить свой ворклог. Для продолжения работы нажмите любую клавишу.")

    return days


def get_jira_worklogs(jira: JIRA, basic_auth: HTTPBasicAuth, days: list[Day], username: str) -> dict[str, tuple[int, list]]:
    # полагаемся на то, что даты предсортированы в методе get_weekdays
    first_date, last_date = days[0].date, days[len(days) - 1].date
    search_dates = [day.date.strftime('%Y-%m-%d') for day in days]
    username = username.lower()

    worklog_url = f"{jira.server_url}/secure/TimesheetReport.jspa"
    headers = {
        "Accept": "application/json"
    }
    params = {
        "reportKey": "jira-timesheet-plugin:report",
        "reportingDay": 0,
        "startDate": f"{first_date.date().strftime("%Y-%m-%d")}",
        "endDate": f"{last_date.date().strftime("%Y-%m-%d")}",
        "sum": "day",
        "moreFields": "assignee",
        "sortBy": "",
        "sorsortDirtBy": "ASC",
        "targetUser": username,
    }

    response = requests.get(worklog_url, headers=headers, params=params, auth=basic_auth)

    if response.status_code != 200:
        logging.error(f"Ошибка при попытке получить ворклог, расчет будет произведет без учета этого: {response.status_code} - {response.text}")
        return dict()

    data = BeautifulSoup(response.text, features="html.parser")
    issues = [element.text.lstrip().rstrip() for element in data.select('tr > td:nth-of-type(3) > a')]
    time_spent_per_day = dict()

    for issue in issues:
        worklogs = jira.worklogs(issue)

        for _worklog in worklogs:
            worklog_date = _worklog.started[:10]

            if _worklog.author.name == username and worklog_date in search_dates:
                if worklog_date not in time_spent_per_day:
                    time_spent_per_day[worklog_date] = (_worklog.timeSpentSeconds, [f"{issue}-{convert_seconds_to_full_time(_worklog.timeSpentSeconds)}"])
                else:
                    time_spent_per_day[worklog_date] = (time_spent_per_day[worklog_date][0] + _worklog.timeSpentSeconds, time_spent_per_day[worklog_date][1] + [f"{issue}-{convert_seconds_to_full_time(_worklog.timeSpentSeconds)}"])

    return time_spent_per_day


def prepare_worklog(issues: dict[str, object], weekdays: list[Day]) -> list[__PreparedWorklog]:
    logging.info('Обработка ворклога перед отправкой в Jira')

    prepared_worklogs = []
    issue_len = len(issues)
    last_index = issue_len - 1

    for day in weekdays:
        str_time_by_issue = []
        time_per_issue = day.fact_time_spent // 3600 // issue_len * 3600
        last_time = day.fact_time_spent - time_per_issue * (issue_len - 1)

        if time_per_issue == 0 and last_time == 0:
            continue

        for index, issue in enumerate(issues):
            comment = issues[issue]

            if last_index == index:
                full_time = convert_seconds_to_full_time(last_time)
                prepared_worklogs.append(__PreparedWorklog(issue=issue, comment=comment, time_spent=full_time,
                                                           day=day.date, int_time_spent=last_time))
            else:
                full_time = convert_seconds_to_full_time(time_per_issue)
                prepared_worklogs.append(__PreparedWorklog(issue=issue, comment=comment, time_spent=full_time,
                                                           day=day.date, int_time_spent=time_per_issue))

            if comment is not None:
                log_str = f"{issue} {comment}-{full_time}"
            else:
                log_str = f"{issue}-{full_time}"

            str_time_by_issue.append(log_str)

        logging.info(f"Формирую ворклог для {day.date.strftime("%d.%m")}: {", ".join(str_time_by_issue)}")

    logging.info("Ворклог обработан")

    return prepared_worklogs


def push_to_jira(p_worklog: list[__PreparedWorklog], jira: JIRA):
    is_push_to_jira = get_yes_no_input("Вы хотите отправить результат в Jira? (yes/y/no/n): ")

    if is_push_to_jira:
        logging.info(f"Отправляю результат")

        for log in p_worklog:
            if not log.is_empty():
                jira.add_worklog(issue=log.issue, timeSpent=log.time_spent, started=log.day, comment=log.comment)
    else:
        logging.info(f"Хорошо, отправка не произведена")


def worklog(jira_url: str, login: str, psswrd: str, year: int, month: int, first_day: int, last_day: int, time_spent: int,
            issues: dict[str, object], alt_day_off: int):
    logging.info('Начало работы скрипта')

    first_day_of_month, last_day_of_month = get_first_and_last_days(year=year, month=month, first_day=first_day,
                                                                    last_day=last_day)
    Day.user_plan_time_spent = time_spent

    logging.info(f'Пользователь {login} планирует заполнить ворклог по {convert_seconds_to_full_time(Day.user_plan_time_spent)} '
                 f'за период с {first_day_of_month.strftime("%d.%m.%Y")} по {last_day_of_month.strftime("%d.%m.%Y")}.')
    logging.info(f'Время будет распределено на следующие задачи: {', '.join(issues)}')

    jira, basic_auth_jira = connect_to_jira(jira_url=jira_url, login=login, psswrd=psswrd)
    weekdays = get_weekdays(first_day=first_day_of_month, last_day=last_day_of_month, alt_day_off=alt_day_off)
    weekdays = fill_time_spent(days=weekdays, jira=jira, basic_auth_jira=basic_auth_jira, username=login)
    prepared_worklogs = prepare_worklog(issues=issues, weekdays=weekdays)
    push_to_jira(p_worklog=prepared_worklogs, jira=jira)

    logging.info("Работа завершена, хорошего дня!")


def get_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Скрипт-помошник логирования времени в Jira')
    parser.add_argument('-c', '--config', required=False, default='config.yaml', help='Путь к конфигурационному файлу YAML')

    args, _ = parser.parse_known_args()
    config = {}

    if os.path.exists(args.config):
        with open(args.config, "r") as f:
            config = yaml.safe_load(f)
    else:
        logging.warning(f"Конфигурационный файл {args.config} не найден. Используются значения по умолчанию.")

    parser.add_argument('-j', '--jira_url',
                        required=False,
                        default=get_or_else(config.get('jira_url'), ''),
                        type=str,
                        help='Jira URL')
    parser.add_argument('-l', '--login',
                        required=False,
                        default=get_or_else(config.get('login'), ''),
                        type=str,
                        help='Логин Jira')
    parser.add_argument('-y', '--year',
                        required=False,
                        default=get_or_else(config.get('year'), datetime.now().year),
                        type=int,
                        help='Опционально указывается логируемый год. По умолчанию текущий')
    parser.add_argument('-m', '--month',
                        required=False,
                        default=get_or_else(config.get('month'), datetime.now().month),
                        type=int,
                        help='Опционально указывается логируемый месяц. По умолчанию текущий')
    parser.add_argument('-fd', '--first-day',
                        required=False,
                        default=get_or_else(config.get('first_day'), 1),
                        type=int,
                        help='Опциональная левая граница интервала логируемого периода. По умолчанию первый день месяца')
    parser.add_argument('-ld', '--last-day',
                        required=False,
                        default=config.get('last_day', None),
                        type=int,
                        help='Опциональная правая граница интервала логируемого периода. По умолчанию последний день месяца')
    parser.add_argument('-H', '--hours',
                        required=False,
                        default=get_or_else(config.get('hours'), 8),
                        type=int,
                        help='Опциональное количество списываемых часов. По умолчанию 8')
    parser.add_argument('-M', '--minutes',
                        required=False,
                        default=get_or_else(config.get('minutes'), 0),
                        type=int,
                        help='Опциональное количество списываемых минут. По умолчанию 0')
    parser.add_argument('--alt-day-off',
                        required=False,
                        default=get_or_else(config.get('alt_day_off'), 0),
                        type=int,
                        help='При возникновении ошибок вендора https://www.isdayoff.ru/ можно игнорировать ошибки. По умолчанию 0, в значении 1 будет использован альтернативный способ')
    parser.add_argument('-i', '--issues',
                        required=False,
                        default=json.dumps(get_or_else(config.get('issues'), {})),
                        help='Мапа задача:комментарий в количестве от одной штуки в формате json')

    args = parser.parse_args()

    return args


def validate_args(args: argparse.Namespace):
    str_errors = list()

    if not is_valid_http_url(args.jira_url):
        str_errors.append("jira url должен соответствовать стандартам HTTP. Например 'https://jira.com/'")

    if re.match(r'^[a-zA-Z]{2,24}$', args.login) is None:
        str_errors.append("login должен соответствовать логину jira")

    if args.year < datetime.now().year - 1 or args.year > datetime.now().year:
        str_errors.append("year должен быть не больше текущего и не меньше предыдущего")

    if args.month < 1 or args.month > 12:
        str_errors.append("month должен быть в диапазоне 1-12")

    if args.first_day < 1 or args.first_day > 31:
        str_errors.append("first_day должен быть в диапазоне 1-31")

    if args.last_day is not None and (args.last_day < 1 or args.last_day > 31):
        str_errors.append("last_day должен быть в диапазоне 1-31")

    if args.hours < 0 or args.hours > 8:
        str_errors.append("hours должен быть в диапазоне 0-8")

    if args.minutes < 0 or args.minutes > 59:
        str_errors.append("minutes должен быть в диапазоне 0-59")

    if args.alt_day_off != 0 and args.alt_day_off != 1:
        str_errors.append("alt_day_off должен быть 1 или 0")

    try:
        args.issues = json.loads(args.issues)
    except:
        str_errors.append("issues имеет невалидную структуру")

    if len(args.issues) == 0:
        str_errors.append("issues должен быть не пустым")

    for issue in args.issues:
        if re.match(r'^[a-zA-Z]{1,10}-[0-9]{1,10}$', issue) is None:
            str_errors.append("issues содержит невалидные данные")
            break

    if len(str_errors) != 0:
        logging.error(f"Ошибка формата при вводе данных: \n{"\n".join(str_errors)}")
        exit(1)


if __name__ == '__main__':
    args = get_args()
    password = getpass.getpass(prompt='Пароль жиры (секюрно 100%): ')
    validate_args(args=args)
    time_spent_seconds = convert_full_time_to_seconds(hours=args.hours, minutes=args.minutes)
    worklog(jira_url=args.jira_url, login=args.login, psswrd=password, year=args.year, month=args.month,
            first_day=args.first_day,last_day=args.last_day, time_spent=time_spent_seconds, issues=args.issues,
            alt_day_off=args.alt_day_off)
