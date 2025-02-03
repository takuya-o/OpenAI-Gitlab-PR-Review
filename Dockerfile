FROM python:3.11-slim-bookworm AS build-env
# 3.11.11-slim-bookworm, 3.11-slim-bookworm, 3.11.11-slim, 3.11-slim⁠
# 3.13.1-slim-bookworm, 3.13-slim-bookworm, 3-slim-bookworm, slim-bookworm, 3.13.1-slim, 3.13-slim, 3-slim, slim⁠

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install -r requirements.txt gunicorn

COPY main.py .

# https://github.com/GoogleContainerTools/distroless
# For debug: docker run -it --entrypoint=sh $CI_REGISTRY_IMAGE
# Version: docker run -it gcr.io/distroless/python3-debian12:debug-nonroot
FROM gcr.io/distroless/python3-debian12:nonroot
# python 3.11.2
COPY --from=build-env /usr/local/lib /usr/local/lib
COPY --from=build-env /usr/local/bin /usr/local/bin
COPY --chown=nonroot:nonroot --from=build-env /app /app
WORKDIR /app

EXPOSE 80

ENTRYPOINT ["gunicorn", "--bind", "0.0.0.0:80", "--timeout", "55", "main:app"]
