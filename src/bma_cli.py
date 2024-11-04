"""The BMA CLI wrapper."""

import json
import logging
import sys
import time
import uuid
from io import BytesIO
from pathlib import Path
from typing import TypedDict

import click
import typer
from bma_client import BmaClient

APP_NAME = "bma-cli"
app = typer.Typer()
app_dir = typer.get_app_dir(APP_NAME)
config_path = Path(app_dir) / "bma_cli_config.json"

logger = logging.getLogger("bma_cli")

# configure loglevel
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s.%(funcName)s():%(lineno)i:  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %z",
)
logging.getLogger("bma_cli").setLevel(logging.DEBUG)
logging.getLogger("bma_client").setLevel(logging.DEBUG)


class BaseJob(TypedDict):
    """Base class inherited by ImageConversionJob and ImageExifExtractionJob."""

    job_type: str
    uuid: uuid.UUID
    basefile_uuid: uuid.UUID
    user_uuid: uuid.UUID
    client_uuid: uuid.UUID
    useragent: str
    finished: bool


class ImageConversionJob(BaseJob):
    """Represent an ImageConversionJob."""

    filetype: str
    width: int
    aspect_ratio_numerator: int | None = None
    aspect_ratio_denominator: int | None = None


class ImageExifExtractionJob(BaseJob):
    """Represent an ImageExifExtractionJob."""


@app.command()
def fileinfo(file_uuid: uuid.UUID) -> None:
    """Get info for a file."""
    client, config = init()
    info = client.get_file_info(file_uuid=file_uuid)
    click.echo(json.dumps(info))


@app.command()
def download(file_uuid: uuid.UUID) -> None:
    """Download a file."""
    client, config = init()
    fileinfo = client.download(file_uuid=file_uuid)
    path = Path(config["path"], fileinfo["filename"])
    click.echo(f"File downloaded to {path}")


@app.command()
def grind() -> None:
    """Get jobs from the server and handle them."""
    client, config = init()

    # get any unfinished jobs already assigned to this client
    jobs = client.get_jobs(job_filter=f"?limit=0&finished=false&client_uuid={client.uuid}")
    if not jobs:
        # no unfinished jobs assigned to this client, ask for new assignment
        jobs = client.get_job_assignment()

    if not jobs:
        click.echo("Nothing to do.")
        return

    # loop over jobs and handle each
    for job in jobs:
        # make sure we have the original file locally
        fileinfo = client.download(file_uuid=job["basefile_uuid"])
        path = Path(config["path"], fileinfo["filename"])
        handle_job(f=path, job=job, client=client, config=config)
    click.echo("Done!")


@app.command()
def upload(files: list[str]) -> None:
    """Loop over files and upload each."""
    client, config = init()
    for f in files:
        pf = Path(f)
        click.echo(f"Uploading file {f}...")
        result = client.upload_file(path=pf, file_license=config["license"], attribution=config["attribution"])
        metadata = result["bma_response"]
        click.echo(f"File {metadata['uuid']} uploaded OK!")
        # check for jobs
        if metadata["jobs_unfinished"] == 0:
            continue

        # it seems there is work to do! ask for assignment
        jobs = client.get_job_assignment(file_uuid=metadata["uuid"])
        if not jobs:
            click.echo("No unassigned unfinished jobs found for this file.")
            continue

        # the grind
        click.echo(f"Handling {len(jobs)} jobs for file {pf} ...")
        for j in jobs:
            # load job in a typeddict, but why?
            klass = getattr(sys.modules[__name__], j["job_type"])
            job = klass(**j)
            handle_job(f=pf, job=job, client=client, config=config)
        click.echo("Done!")


@app.command()
def exif(path: Path) -> None:
    """Get and return exif for a file."""
    client, config = init()
    click.echo(json.dumps(client.get_exif(fname=path)))


@app.command()
def settings() -> None:
    """Get and return settings from the BMA server."""
    client, config = init()
    click.echo(json.dumps(client.get_server_settings()))


def handle_job(f: Path, job: ImageConversionJob | ImageExifExtractionJob, client: BmaClient, config: dict) -> None:
    """Handle a job and upload the result."""
    click.echo("======================================================")
    click.echo(f"Handling job {job['job_type']} {job['job_uuid']} ...")
    start = time.time()
    result = client.handle_job(job=job, orig=f)
    logger.debug(f"Getting result took {time.time() - start} seconds")
    if not result:
        click.echo(f"No result returned for job {job['job_type']} {job['uuid']} - skipping ...")
        return

    # got job result, do whatever is needed depending on job_type
    if job["job_type"] == "ImageConversionJob":
        image, exif = result
        filename = job["job_uuid"] + "." + job["filetype"].lower()
        logger.debug(f"Encoding result as {job['filetype']} ...")
        start = time.time()
        with BytesIO() as buf:
            image.save(buf, format=job["filetype"], exif=exif, lossless=False, quality=90)
            logger.debug(f"Encoding result took {time.time() - start} seconds")
            client.upload_job_result(job_uuid=job["job_uuid"], buf=buf, filename=filename)
    elif job["job_type"] == "ImageExifExtractionJob":
        logger.debug(f"Got exif data {result}")
        with BytesIO() as buf:
            buf.write(json.dumps(result).encode())
            client.upload_job_result(job_uuid=job["job_uuid"], buf=buf, filename="exif.json")
    else:
        logger.error("Unsupported job type")
        raise typer.Exit(1)


def load_config() -> dict[str, str]:
    """Load config file."""
    # bail out on missing config
    if not config_path.is_file():
        click.echo(f"Config file {config_path} not found")
        raise typer.Exit(1)

    # read config file
    with config_path.open() as f:
        config = f.read()

    # parse json and return config dict
    return json.loads(config)


def get_client(config: dict[str, str]) -> BmaClient:
    """Initialise client."""
    return BmaClient(
        oauth_client_id=config["oauth_client_id"],
        refresh_token=config["refresh_token"],
        path=Path(config["path"]),
        base_url=config["bma_url"],
        client_uuid=config["client_uuid"],
    )


def init() -> tuple[BmaClient, dict[str, str]]:
    """Load config file and get client."""
    config = load_config()
    logger.debug(f"loaded config: {config}")
    client = get_client(config=config)

    # save refresh token to config
    config["refresh_token"] = client.refresh_token
    logger.debug(f"Wrote updated refresh_token to config: {config}")
    with config_path.open("w") as f:
        f.write(json.dumps(config))
    return client, config


if __name__ == "__main__":
    app()
