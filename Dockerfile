# Built for the Fargate task's platform (linux/amd64 by default; see cpu_architecture in Terraform).
# usptrader-ops push-image reads the platform from the task definition.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
COPY --from=build /install /usr/local
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin trader
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["usptrader"]
CMD ["--help"]
