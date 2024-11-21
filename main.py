from dataclasses import dataclass
from typing import List, Optional, Dict
import openai
from datetime import datetime, timedelta
import requests
from collections import defaultdict
import csv
from base64 import b64encode
from dotenv import load_dotenv
import os
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TimeElapsedColumn
from rich.logging import RichHandler
import logging

console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, rich_tracebacks=True)],
)
logger = logging.getLogger("work_logger")


@dataclass
class WakaTimeConfig:
    api_key: str
    base_url: str = "https://wakatime.com/api/v1"

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Basic {b64encode(f'{self.api_key}:'.encode()).decode()}"
        }


@dataclass
class DurationEntry:
    entity: str
    type: str
    time: float
    project: str
    project_root_count: int
    branch: str
    language: str
    dependencies: List[str]
    duration: float


@dataclass
class DurationsResponse:
    data: List[DurationEntry]
    start: datetime
    end: datetime
    timezone: str
    branches: List[str]
    available_branches: List[str]
    color: Optional[str] = None

    @property
    def total_duration(self) -> float:
        return sum(float(entry.duration) for entry in self.data)

    @classmethod
    def from_dict(cls, data: Dict) -> "DurationsResponse":
        duration_entries = []
        for entry_data in data["data"]:
            duration_entry = DurationEntry(
                entity=str(entry_data["entity"]),
                type=str(entry_data["type"]),
                time=float(entry_data["time"]),
                project=str(entry_data["project"]),
                project_root_count=int(
                    entry_data["project_root_count"]
                    if entry_data["project_root_count"]
                    else 0
                ),
                branch=str(entry_data["branch"]),
                language=str(entry_data["language"]),
                dependencies=list(entry_data["dependencies"]),
                duration=float(entry_data["duration"]),
            )
            duration_entries.append(duration_entry)

        return cls(
            data=duration_entries,
            start=datetime.fromisoformat(data["start"].replace("Z", "+00:00")),
            end=datetime.fromisoformat(data["end"].replace("Z", "+00:00")),
            timezone=data["timezone"],
            color=data.get("color"),
            branches=data["branches"],
            available_branches=data["available_branches"],
        )


class WakaTimeClient:
    def __init__(self, config: WakaTimeConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(config.headers)

    def get_durations(self, project: str, date: datetime) -> DurationsResponse:
        url = f"{self.config.base_url}/users/current/durations"
        params = {"date": date.strftime("%Y-%m-%d"), "project": project}

        response = self.session.get(url, params=params)
        response.raise_for_status()

        return DurationsResponse.from_dict(response.json())


def format_duration(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def generate_work_description(
    openai_client: openai.OpenAI,
    entries: List[DurationEntry],
    project: str,
) -> str:

    entities_and_duration = set()
    for entry in entries:
        if "src/" not in entry.entity:
            continue

        MIN_DURATION_IN_SECONDS = 60

        if entry.duration < MIN_DURATION_IN_SECONDS:
            continue

        name = entry.entity.split("src/")[1]
        duration = format_duration(entry.duration)
        entities_and_duration.add((name, duration))

    prompt = f"""Based on the following information from the {project} project, 
    provide a brief (max 300 chars) summary in brazilian portuguese of the work done:

    - DON'T use words like “several”, “various”. You should be precise and say exactly what was done.
    - DON'T mention what the project is about (for example, saying that SIPE is HR software)
    - DON'T mention the duration of the work in each file 

    Good description: "Listar empresas cadastradas, cadastrar dispositivos e permitir autenticação via token. Também foram feitas modificações nos processos de autorização e na seleção de métodos de pagamento na assinatura.", "Alterações nos arquivos relacionados à gestão de assinaturas e locatários, incluindo otimizações em endpoints, validações de dados e definição de preço para plano experimental como zero. Novas funcionalidades como tratamento de erros para tenantes inexistentes e verificação de duplicidade de CNPJ.
    Bad description exercepts: "Este trabalho contribui para aprimorar a integridade e funcionalidade dos serviços SIPE, um software de RH para controle de ponto dos colaboradores.", "Foram feitas diversas atualizações nos arquivos de código-fonte de diversos componentes do projeto SIPE"

    Files worked on (with duration):
    {", ".join(f"{entity} ({duration})" for entity, duration in entities_and_duration)}
    
    Note: This is part of a microservices architecture where:
    - sipe-web is the frontend service
    - sipe-api is the backend service

    Also, SIPE is HR software for employees to clock in and out.
    
    Summary:
    """

    response = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": "You are a technical writer summarizing development work.",
            },
            {"role": "user", "content": prompt},
        ],
    )

    return response.choices[0].message.content


def get_weekday(date: datetime) -> str:
    weekdays = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]
    return weekdays[date.weekday()]


def analyze_and_write_csv(
    openai_client: openai.OpenAI,
    wakatime_client: WakaTimeClient,
    projects: List[str],
    start_date: datetime,
    end_date: datetime,
):
    logger.info(f"Starting analysis from {start_date} to {end_date}")

    with open("trabalho.csv", "w", newline="", encoding="UTF-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Data", "Horas", "Descrição"])

        days_total = (end_date - start_date).days + 1

        with Progress(
            SpinnerColumn(),
            *Progress.get_default_columns(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("[cyan]Processing days...", total=days_total)

            current_date = start_date
            while current_date <= end_date:
                progress.console.print(f"[yellow]Processing {current_date.date()}")
                daily_entries = defaultdict(list)
                total_duration = 0.0

                for project in projects:
                    try:
                        progress.console.print(
                            f"[blue]Fetching WakaTime data for {project}"
                        )
                        durations = wakatime_client.get_durations(project, current_date)
                        total_duration += durations.total_duration
                        daily_entries[current_date].extend(durations.data)
                    except requests.RequestException as e:
                        logger.error(
                            f"Error fetching WakaTime data for {project} on {current_date}: {e}"
                        )

                if daily_entries[current_date]:
                    progress.console.print("[magenta]Generating work description")
                    description = generate_work_description(
                        openai_client,
                        daily_entries[current_date],
                        ", ".join(projects),
                    )

                    date = "{} ({})".format(
                        current_date.strftime("%Y-%m-%d"), get_weekday(current_date)
                    )

                    writer.writerow(
                        [
                            date,
                            format_duration(total_duration),
                            description,
                        ]
                    )

                current_date += timedelta(days=1)
                progress.advance(task)

    logger.info("Analysis completed! CSV file has been generated.")


def is_environment_valid():
    return all(
        [
            os.getenv("OPENAI_API_KEY"),
            os.getenv("WAKATIME_TOKEN"),
        ]
    )


def main():
    console.print("[bold green]Starting work analysis...[/bold green]")

    load_dotenv()
    logger.info("Loading environment variables...")

    if not is_environment_valid():
        logger.error(
            "Please make sure you have the following environment variables set: OPENAI_API_KEY, WAKATIME_TOKEN, GITHUB_TOKEN"
        )
        return

    openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    wakatime_config = WakaTimeConfig(api_key=os.getenv("WAKATIME_TOKEN"))
    wakatime_client = WakaTimeClient(wakatime_config)

    projects = ["sipe-web", "sipe-api", "sipe-api-2", "sipe-api-3"]

    end_date = datetime.now()
    start_date = datetime(2024, 10, 21)  # Data de início

    logger.info(f"Analyzing work from {start_date.date()} to {end_date.date()}")

    analyze_and_write_csv(
        openai_client,
        wakatime_client,
        projects,
        start_date,
        end_date,
    )


if __name__ == "__main__":
    main()
