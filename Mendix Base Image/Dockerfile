# Base pinned to an immutable digest for reproducible, scannable builds (NAAAPS).
# Refresh: docker pull eclipse-temurin:21-jdk && docker inspect --format '{{index .RepoDigests 0}}' eclipse-temurin:21-jdk
FROM eclipse-temurin:21-jdk@sha256:762d16be48b976c97801d198e5085b1f6facb13718a1e8fb67cb25d0b25337e1
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client unzip && rm -rf /var/lib/apt/lists/*
WORKDIR /mendix
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh
EXPOSE 8080 8090
CMD ["./entrypoint.sh"]
