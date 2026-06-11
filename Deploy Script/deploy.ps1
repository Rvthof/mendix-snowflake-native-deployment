# deploy.ps1 - Deploy a Mendix PAD package to Snowpark Container Services
#
# Usage:
#   .\deploy.ps1 -PadPath "C:\path\to\MyApp_portable.zip"
#   .\deploy.ps1                              # prompts for path
#   .\deploy.ps1 -Config "my-config.json"     # use a different config file
#
# Configuration:
#   All settings are read from deploy-config.json (or the file specified by -Config).
#   Constants are managed in deploy-config-constants.json (auto-generated from PAD).
#
# Prerequisites:
#   - Rancher Desktop (or Docker Desktop) running with dockerd engine
#   - Docker logged in to the Snowflake registry
#   - Snowflake CLI (`snow`) installed and connection configured

param(
    [string]$PadPath,
    [string]$Config = "deploy-config.json"
)

$ErrorActionPreference = "Stop"
$cleanupPath = $null

# ============================================================
# Load configuration
# ============================================================
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $scriptDir $Config }

if (-not (Test-Path $configPath)) {
    Write-Error "Config file not found: $configPath`nCopy deploy-config.json and fill in your values."
    exit 1
}

$cfg = Get-Content $configPath -Raw | ConvertFrom-Json

# Extract config values
$SnowConnection = $cfg.snowConnection
$ServiceName    = $cfg.service.name
$ImageRepo      = $cfg.service.imageRepo
$ImageName      = $cfg.service.imageName
$EAI            = $cfg.service.externalAccessIntegration

$DbHost         = $cfg.database.host
$DbPort         = $cfg.database.port
$DbName         = $cfg.database.name
$DbUser         = $cfg.database.username
$DbPass         = $cfg.database.password
$DbSsl          = $cfg.database.useSsl

$AdminPass      = $cfg.mendix.adminPassword
$FileStage      = $cfg.mendix.fileStorageStage

$MemRequest     = $cfg.resources.memory.request
$MemLimit       = $cfg.resources.memory.limit
$CpuRequest     = $cfg.resources.cpu.request
$CpuLimit       = $cfg.resources.cpu.limit

# Derive database.schema from ServiceName (e.g. "DB.SCHEMA.SERVICE" -> "DB.SCHEMA")
$serviceParts = $ServiceName -split '\.'
$ServiceDbSchema = "$($serviceParts[0]).$($serviceParts[1])"

# Derive registry host from snow CLI connection
Write-Host "Loading registry URL from snow CLI..." -ForegroundColor DarkGray
$registryRaw = snow spcs image-registry url --connection $SnowConnection 2>$null
$RegistryHost = ($registryRaw | Out-String).Trim()
if (-not $RegistryHost) {
    Write-Error "Could not determine registry URL from snow CLI connection '$SnowConnection'"
    exit 1
}

Write-Host "  Registry: $RegistryHost" -ForegroundColor DarkGray
Write-Host "  Service:  $ServiceName" -ForegroundColor DarkGray
Write-Host ""

# ============================================================
# Helper: Parse PAD constants
# ============================================================
function Get-PadConstants {
    param([string]$BuildContext)

    $defaultsFile = Join-Path $BuildContext "etc\constants\defaults.conf"
    $variablesFile = Join-Path $BuildContext "etc\constants\variables.conf"

    if (-not (Test-Path $defaultsFile) -or -not (Test-Path $variablesFile)) {
        return @()
    }

    $defaults = @{}
    $envVars = @{}

    # Parse defaults.conf: "Module.Name" = value
    foreach ($line in Get-Content $defaultsFile) {
        if ($line -match '^\s*"([^"]+)"\s*=\s*(.*)$') {
            $name = $Matches[1]
            $val = $Matches[2].Trim()
            # Strip surrounding quotes if present
            if ($val -match '^"(.*)"$') { $val = $Matches[1] }
            $defaults[$name] = $val
        }
    }

    # Parse variables.conf: "Module.Name" = ${?ENV_VAR_NAME}
    foreach ($line in Get-Content $variablesFile) {
        if ($line -match '^\s*"([^"]+)"\s*=\s*\$\{\?([^}]+)\}') {
            $name = $Matches[1]
            $envVar = $Matches[2]
            $envVars[$name] = $envVar
        }
    }

    # Build result list
    $constants = @()
    foreach ($name in $defaults.Keys) {
        if ($envVars.ContainsKey($name)) {
            $constants += [PSCustomObject]@{
                Name       = $name
                EnvVar     = $envVars[$name]
                Default    = $defaults[$name]
                SecretName = "MX_CONST_" + ($name -replace '\.', '_').ToUpper()
            }
        }
    }

    return $constants
}

# ============================================================
# Helper: Manage constants config file
# ============================================================
function Sync-ConstantsConfig {
    param(
        [array]$Constants,
        [string]$ConfigDir
    )

    $constantsConfigPath = Join-Path $ConfigDir "deploy-config-constants.json"
    $existing = @{}

    if (Test-Path $constantsConfigPath) {
        $raw = Get-Content $constantsConfigPath -Raw | ConvertFrom-Json
        if ($raw.constants) {
            foreach ($prop in $raw.constants.PSObject.Properties) {
                $existing[$prop.Name] = $prop.Value
            }
        }
    }

    # Merge: keep existing values, add new constants with PAD defaults
    $merged = [ordered]@{}
    foreach ($c in $Constants | Sort-Object Name) {
        if ($existing.ContainsKey($c.Name)) {
            $merged[$c.Name] = $existing[$c.Name]
        } else {
            $merged[$c.Name] = $c.Default
        }
    }

    # Write back
    $output = [ordered]@{
        "// INSTRUCTIONS" = "Fill in values for constants with empty strings. Save the file, then press Enter in the deploy script."
        constants = $merged
    }
    $output | ConvertTo-Json -Depth 5 | Set-Content -Path $constantsConfigPath -Encoding UTF8

    return $constantsConfigPath
}

# ============================================================
# Helper: Create/update Snowflake secrets
# ============================================================
function Sync-SnowflakeSecrets {
    param(
        [array]$Constants,
        [hashtable]$Values,
        [string]$DbSchema,
        [string]$Connection
    )

    $sqlStatements = @()
    foreach ($c in $Constants) {
        $val = $Values[$c.Name]
        # Escape single quotes in value
        $escapedVal = $val -replace "'", "''"
        $fqn = "$DbSchema.$($c.SecretName)"
        $sqlStatements += "CREATE OR REPLACE SECRET $fqn TYPE = GENERIC_STRING SECRET_STRING = '$escapedVal';"
    }

    $sqlFile = Join-Path $env:TEMP "mendix-secrets-$(Get-Date -Format 'yyyyMMdd-HHmmss').sql"
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($sqlFile, ($sqlStatements -join "`n"), $utf8NoBom)

    try {
        $output = cmd /c "snow sql -f `"$sqlFile`" --connection $Connection --format json --enable-templating NONE 2>&1"
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            Write-Error "Failed to create/update Snowflake secrets. Check permissions (need CREATE SECRET on schema).`n$output"
        }
    } finally {
        Remove-Item $sqlFile -Force -ErrorAction SilentlyContinue
    }
}

# ============================================================
# Helper: Generate secrets YAML block for the service spec
# ============================================================
function Get-SecretsYaml {
    param(
        [array]$Constants,
        [string]$DbSchema
    )

    if ($Constants.Count -eq 0) { return "" }

    $lines = @("    secrets:")
    foreach ($c in $Constants) {
        $fqn = "$DbSchema.$($c.SecretName)"
        $lines += "    - snowflakeSecret: $fqn"
        $lines += "      secretKeyRef: secret_string"
        $lines += "      envVarName: $($c.EnvVar)"
    }

    return ($lines -join "`n")
}

# ============================================================
# Deploy
# ============================================================
try {

# Prompt for PAD path if not provided
if (-not $PadPath) {
    $PadPath = Read-Host "Enter path to PAD zip file or extracted folder"
}

# Resolve to absolute path
$PadPath = Resolve-Path $PadPath

# Determine if it's a ZIP or a directory
if (Test-Path $PadPath -PathType Leaf) {
    if ($PadPath -notmatch '\.zip$') {
        Write-Error "File must be a .zip archive: $PadPath"
        exit 1
    }

    Write-Host "[1/6] Extracting PAD package..." -ForegroundColor Cyan
    $ExtractDir = Join-Path $env:TEMP "mendix-pad-build-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    $cleanupPath = $ExtractDir
    Expand-Archive -Path $PadPath -DestinationPath $ExtractDir -Force

    $children = Get-ChildItem $ExtractDir
    if ($children.Count -eq 1 -and $children[0].PSIsContainer) {
        $BuildContext = $children[0].FullName
    } else {
        $BuildContext = $ExtractDir
    }
} elseif (Test-Path $PadPath -PathType Container) {
    Write-Host "[1/6] Using existing folder (no extraction needed)" -ForegroundColor Cyan
    $BuildContext = $PadPath
} else {
    Write-Error "Path not found: $PadPath"
    exit 1
}

# Verify it looks like a PAD package
if (-not (Test-Path "$BuildContext\bin\start")) {
    Write-Error "Not a valid PAD package: missing bin/start in $BuildContext"
    exit 1
}

# ============================================================
# Step 2: Configure constants
# ============================================================
Write-Host "[2/6] Configuring app constants..." -ForegroundColor Cyan

$padConstants = Get-PadConstants -BuildContext $BuildContext

if ($padConstants.Count -gt 0) {
    Write-Host "  Found $($padConstants.Count) constant(s) in PAD" -ForegroundColor DarkGray

    # Sync the constants config file
    $constantsConfigPath = Sync-ConstantsConfig -Constants $padConstants -ConfigDir $scriptDir

    # Load values and check for empties
    $constConfig = Get-Content $constantsConfigPath -Raw | ConvertFrom-Json
    $constValues = @{}
    $empties = @()
    foreach ($prop in $constConfig.constants.PSObject.Properties) {
        $constValues[$prop.Name] = $prop.Value
        if ([string]::IsNullOrEmpty($prop.Value)) {
            $empties += $prop.Name
        }
    }

    # Prompt user if there are empty values
    if ($empties.Count -gt 0) {
        Write-Host ""
        Write-Host "  The following constants need values:" -ForegroundColor Yellow
        foreach ($e in $empties) {
            Write-Host "    - $e" -ForegroundColor Yellow
        }
        Write-Host ""
        Write-Host "  Edit: $constantsConfigPath" -ForegroundColor White
        Read-Host "  Fill in the values, save the file, then press Enter to continue"

        # Re-read
        $constConfig = Get-Content $constantsConfigPath -Raw | ConvertFrom-Json
        $constValues = @{}
        $stillEmpty = @()
        foreach ($prop in $constConfig.constants.PSObject.Properties) {
            $constValues[$prop.Name] = $prop.Value
            if ([string]::IsNullOrEmpty($prop.Value)) {
                $stillEmpty += $prop.Name
            }
        }

        if ($stillEmpty.Count -gt 0) {
            Write-Error "Constants still have empty values: $($stillEmpty -join ', '). Aborting."
            exit 1
        }
    }

    # Create/update Snowflake secrets
    Write-Host "  Creating/updating Snowflake secrets..." -ForegroundColor DarkGray
    Sync-SnowflakeSecrets -Constants $padConstants -Values $constValues -DbSchema $ServiceDbSchema -Connection $SnowConnection
    Write-Host "  Secrets synced." -ForegroundColor DarkGray
} else {
    Write-Host "  No constants found in PAD." -ForegroundColor DarkGray
}

# ============================================================
# Step 3: Build Docker image
# ============================================================

# Create Dockerfile if not present
$DockerfilePath = "$BuildContext\Dockerfile"
if (-not (Test-Path $DockerfilePath)) {
    Write-Host "  Creating Dockerfile..." -ForegroundColor DarkGray
    @"
FROM eclipse-temurin:21-jdk
WORKDIR /mendix
COPY ./app ./app
COPY ./bin ./bin
COPY ./etc ./etc
COPY ./lib ./lib
ENV MX_LOG_LEVEL=info
EXPOSE 8080 8090
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh
CMD ["./entrypoint.sh"]
"@ | Set-Content -Path $DockerfilePath -Encoding UTF8
}

# Create entrypoint script that resolves {SNOWFLAKE_HOST} placeholder in env vars
$EntrypointPath = "$BuildContext\entrypoint.sh"
if (-not (Test-Path $EntrypointPath)) {
    $entrypointContent = @"
#!/bin/bash
# Resolve {SNOWFLAKE_HOST} placeholder in any CONSTANTS_* env vars
for var in `$(env | grep '^CONSTANTS_' | cut -d= -f1); do
    val="`${!var}"
    if [[ "`$val" == *"{SNOWFLAKE_HOST}"* ]]; then
        export "`$var"="`${val//\{SNOWFLAKE_HOST\}/`$SNOWFLAKE_HOST}"
    fi
done
exec ./bin/start etc/Default
"@
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($EntrypointPath, $entrypointContent.Replace("`r`n", "`n"), $utf8NoBom)
}

Write-Host "[3/6] Building Docker image..." -ForegroundColor Cyan
$tag = "${ImageName}:$(Get-Date -Format 'yyyyMMdd-HHmm')"
docker build --platform linux/amd64 -t $tag "$BuildContext"
if ($LASTEXITCODE -ne 0) { Write-Error "Docker build failed" }

# ============================================================
# Step 4: Push to registry
# ============================================================
Write-Host "[4/6] Pushing to Snowflake registry..." -ForegroundColor Cyan
$fullTag = "$RegistryHost/$ImageRepo/${ImageName}:latest"
docker tag $tag $fullTag
docker push $fullTag
if ($LASTEXITCODE -ne 0) { Write-Error "Docker push failed. Are you logged in? Run: docker login $RegistryHost" }

# ============================================================
# Step 5: Update service spec
# ============================================================
Write-Host "[5/6] Updating service (ALTER SERVICE FROM SPECIFICATION)..." -ForegroundColor Cyan
$sslValue = if ($DbSsl) { "true" } else { "false" }

# Generate secrets YAML block
$secretsBlock = ""
if ($padConstants.Count -gt 0) {
    $secretsBlock = Get-SecretsYaml -Constants $padConstants -DbSchema $ServiceDbSchema
}

# Write SQL to a temp file to avoid PowerShell mangling the $$ dollar-quoting
$sqlFile = Join-Path $env:TEMP "mendix-alter-service-$(Get-Date -Format 'yyyyMMdd-HHmmss').sql"
$specSql = @"
ALTER SERVICE $ServiceName FROM SPECIFICATION `$`$
spec:
  containers:
  - name: mendix-app
    image: /$ImageRepo/${ImageName}:latest
    env:
      RUNTIME_PARAMS_DATABASETYPE: "POSTGRESQL"
      RUNTIME_PARAMS_DATABASEHOST: "${DbHost}:${DbPort}"
      RUNTIME_PARAMS_DATABASENAME: "$DbName"
      RUNTIME_PARAMS_DATABASEUSERNAME: "$DbUser"
      RUNTIME_PARAMS_DATABASEPASSWORD: "$DbPass"
      RUNTIME_PARAMS_DATABASEUSESSL: "$sslValue"
      RUNTIME_PARAMS_COM_MENDIX_CORE_STORAGESERVICE: "com.mendix.storage.localfilesystem"
      RUNTIME_PARAMS_UPLOADEDFILESPATH: "/mnt/filestorage"
      M2EE_ADMIN_PASS: "$AdminPass"
      RUNTIME_ADMINUSER_PASSWORD: "$AdminPass"
$secretsBlock
    readinessProbe:
      port: 8080
      path: /
    resources:
      requests:
        memory: $MemRequest
        cpu: $CpuRequest
      limits:
        memory: $MemLimit
        cpu: $CpuLimit
    volumeMounts:
    - name: filestorage
      mountPath: /mnt/filestorage
  volumes:
  - name: filestorage
    source: stage
    stageConfig:
      name: "$FileStage"
  endpoints:
  - name: mendix-web
    port: 8080
    public: true
  logExporters:
    eventTableConfig:
      logLevel: INFO
capabilities:
  securityContext:
    executeAsCaller: true
`$`$;
"@
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($sqlFile, $specSql, $utf8NoBom)

try {
    $output = cmd /c "snow sql -f `"$sqlFile`" --connection $SnowConnection --format json --enable-templating NONE 2>&1"
    $alterResult = $LASTEXITCODE
} finally {
    Remove-Item $sqlFile -Force -ErrorAction SilentlyContinue
}

if ($alterResult -ne 0) {
    Write-Host "  ALTER SERVICE failed. Check that your service exists and EAI is attached." -ForegroundColor Red
    Write-Host "  Attempting suspend/resume as fallback (note: this may not pick up the new image)..." -ForegroundColor DarkYellow
    snow sql -q "ALTER SERVICE $ServiceName SUSPEND;" --connection $SnowConnection --format json 2>&1 | Out-Null
    Start-Sleep -Seconds 5
    snow sql -q "ALTER SERVICE $ServiceName RESUME;" --connection $SnowConnection --format json 2>&1 | Out-Null
}

# ============================================================
# Step 6: Done
# ============================================================
Write-Host "[6/6] Deployed!" -ForegroundColor Green
try {
    $endpointsRaw = snow sql -q "SHOW ENDPOINTS IN SERVICE $ServiceName;" --connection $SnowConnection --format json 2>$null
    $endpointsJson = ($endpointsRaw | Out-String).Trim()
    if ($endpointsJson -match '"ingress_url"\s*:\s*"([^"]+)"') {
        $ingressUrl = $Matches[1]
    }
} catch {
    $ingressUrl = $null
}

Write-Host ""
Write-Host "  Image: $fullTag" -ForegroundColor DarkGray
Write-Host "  Service will be ready in ~2-3 minutes." -ForegroundColor DarkGray
if ($ingressUrl) {
    Write-Host "  URL: https://$ingressUrl" -ForegroundColor Yellow
} else {
    Write-Host "  URL: (provisioning - check with SHOW ENDPOINTS)" -ForegroundColor DarkGray
}

# ============================================================
# Optional: Configure caller grants
# ============================================================
Write-Host ""
$setupGrants = Read-Host "  Set up caller grants for Snowflake data access? (y/N)"
if ($setupGrants -match '^[Yy]') {
    Write-Host ""
    Write-Host "  Caller grants allow the service to query Snowflake on behalf of users." -ForegroundColor DarkGray
    Write-Host "  The service owner role needs CALLER grants for each database/warehouse." -ForegroundColor DarkGray
    Write-Host ""

    # Determine service owner role
    $ownerRole = Read-Host "  Service owner role (default: ACCOUNTADMIN)"
    if (-not $ownerRole) { $ownerRole = "ACCOUNTADMIN" }
    Write-Host "  Using role: $ownerRole" -ForegroundColor DarkGray

    $databases = Read-Host "  Database(s) to grant access to (comma-separated, e.g. MY_DB,OTHER_DB)"
    $warehouse = Read-Host "  Warehouse for query execution (e.g. COMPUTE_WH)"

    if ($warehouse) {
        $grantSql = "GRANT CALLER USAGE ON WAREHOUSE $warehouse TO ROLE $ownerRole;"
        cmd /c "snow sql -q `"$grantSql`" --connection $SnowConnection --format json 2>&1" | Out-Null
        Write-Host "    Granted CALLER USAGE on warehouse $warehouse" -ForegroundColor DarkGray
    }

    foreach ($db in ($databases -split ',')) {
        $db = $db.Trim()
        if (-not $db) { continue }

        $statements = @(
            "GRANT CALLER USAGE ON DATABASE $db TO ROLE $ownerRole;",
            "GRANT INHERITED CALLER USAGE ON ALL SCHEMAS IN DATABASE $db TO ROLE $ownerRole;",
            "GRANT INHERITED CALLER SELECT ON ALL TABLES IN DATABASE $db TO ROLE $ownerRole;",
            "GRANT INHERITED CALLER SELECT ON ALL VIEWS IN DATABASE $db TO ROLE $ownerRole;"
        )

        foreach ($stmt in $statements) {
            cmd /c "snow sql -q `"$stmt`" --connection $SnowConnection --format json 2>&1" | Out-Null
        }
        Write-Host "    Granted CALLER access on database $db (all schemas, tables, views)" -ForegroundColor DarkGray
    }

    Write-Host ""
    Write-Host "  Caller grants configured." -ForegroundColor Green
    Write-Host "  Note: Users must also have the corresponding privileges via their own role." -ForegroundColor DarkGray
}

} finally {
    if ($cleanupPath -and (Test-Path $cleanupPath)) {
        Write-Host "  Cleaning up temp files..." -ForegroundColor DarkGray
        Remove-Item -Recurse -Force $cleanupPath
    }
}
