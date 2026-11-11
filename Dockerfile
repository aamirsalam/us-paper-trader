# Built for linux/arm64 (Fargate Graviton). CI cross-builds with buildx.
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install . \
 && python -m compileall -q /install

FROM python:3.12-slim
COPY --from=build /install /usr/local
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin trader
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["usptrader"]
CMD ["--help"]
