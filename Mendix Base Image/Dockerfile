FROM eclipse-temurin:21-jdk
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client unzip && rm -rf /var/lib/apt/lists/*
WORKDIR /mendix
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh
EXPOSE 8080 8090
CMD ["./entrypoint.sh"]
