"""The BMA CLI wrapper."""

import json
import logging
import uuid
from datetime import UTC, datetime
from importlib.metadata import version as get_version
from pathlib import Path

import click
import typer
from bma_client_lib import BmaClient
from bma_client_lib.datastructures import JobNotSupportedError, job_types

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


@app.command()
def version() -> None:
    """Return the version of bma-cli and bma-client."""
    click.echo(f"bma-cli version {get_version('bma-cli')}")
    click.echo(f"bma-client-lib version {get_version('bma-client-lib')}")


@app.command()
def fileinfo(file_uuid: uuid.UUID) -> None:
    """Get info for a file."""
    client, config = init()
    info = client.get_file_info(file_uuid=file_uuid)
    click.echo(json.dumps(info))


@app.command()
def jobs(filter: str = "?limit=0&finished=false") -> None:  # noqa: A002
    """Get info on unfinished jobs."""
    client, config = init()
    jobs = client.get_jobs(job_filter=filter)
    click.echo(json.dumps(jobs))
    click.echo(f"Total {len(jobs)} jobs returned by filter {filter}.", err=True)


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
    # keep track of failing jobs to prevent getting them assigned again
    failed_jobs: set[str] = set()
    # run in a loop to make sure jobs created as a result of other jobs being completed are all processed
    while True:
        # first get any unfinished jobs already assigned to this client
        job_filter = f"?limit=0&finished=false&client_uuid={client.uuid}"
        if failed_jobs:
            job_filter += f"&skip_jobs={','.join(failed_jobs)}"
        jobs = client.get_jobs(job_filter=job_filter)
        if not jobs:
            # no unfinished jobs assigned to this client, ask for new assignment,
            # but skip jobs which previously failed
            job_filter = f"?skip_jobs={','.join(failed_jobs)}" if failed_jobs else ""
            jobs = client.get_job_assignment(job_filter=job_filter)

        if not jobs:
            click.echo("Nothing left to do.")
            break

        # loop over jobs and handle each
        click.echo(f"Processing {len(jobs)} jobs for file {jobs[0]['basefile_uuid']} ...")
        for dictjob in jobs:
            job = job_types[dictjob["job_type"]](**dictjob)
            click.echo(f"Handling {job.job_type} {job.job_uuid} ...")
            try:
                client.handle_job(job=job)
            except JobNotSupportedError:
                logger.exception(f"Job {job.job_uuid} not supported")
                failed_jobs.add(str(job.job_uuid))
                client.unassign_job(job=job)
                continue
    click.echo("Done grinding for now!")


@app.command()
def upload(files: list[str]) -> None:
    """Loop over files and upload each."""
    client, config = init()
    file_uuids = []
    for f in files:
        pf = Path(f)
        click.echo(f"Uploading file {f}...")
        result = client.upload_file(path=pf, file_license=config["license"], attribution=config["attribution"])
        metadata = result["bma_response"]
        click.echo(f"File {metadata['uuid']} uploaded OK!")
        file_uuids.append(metadata["uuid"])
    click.echo(f"Finished uploading {len(file_uuids)} files, creating album...")
    now = datetime.isoformat(datetime.now(tz=UTC))
    album = client.create_album(file_uuids=file_uuids, title=f"Created-{now}", description=f"Created-{now}")
    url = f"{client.base_url}/albums/{album['uuid']}/"
    if config.get("handle_jobs"):
        grind()
    click.echo(f"Created album {album['uuid']} with the uploaded file(s) see it at {url}")
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
