# build-and-run.ps1
# Usage: Run this PowerShell script after editing Audiveris Java files

# === Configuration ===
$audiverisRoot = "C:\audiveris"
$pythonAppFolder = "C:\Users\fe1pm\Documents\congregational-key-finder"
$venvActivateScript = "$pythonAppFolder\venv\Scripts\Activate.ps1"
$pythonAppScript = "app.py"

Write-Host "Starting Audiveris build and Python app run script..."

# Step 1: Change to Audiveris root
Write-Host "Changing directory to Audiveris project root: $audiverisRoot"
Set-Location $audiverisRoot

# Step 2: Run Gradle clean build
Write-Host "Running Gradle clean build..."
.\gradlew.bat installDist

# Confirm rebuild success
if (!(Test-Path "build\install\app\bin\audiveris.bat")) {
    Write-Host "❌ audiveris.bat not found. Build may have failed." -ForegroundColor Red
    exit 1
}

if ($LASTEXITCODE -ne 0) {
    Write-Error "Gradle build failed! Exiting."
    exit $LASTEXITCODE
}

Write-Host "Gradle build successful."

# Step 3: Change to Python app folder
Write-Host "Changing directory to Python app folder: $pythonAppFolder"
Set-Location $pythonAppFolder

# Step 4: Activate Python virtual environment
Write-Host "Activating Python virtual environment..."
if (Test-Path $venvActivateScript) {
    & $venvActivateScript
} else {
    Write-Error "Virtual environment activation script not found: $venvActivateScript"
    exit 1
}

# Step 5: Run the Python app
Write-Host "Running Python app: $pythonAppScript"
python $pythonAppScript

Write-Host "Script finished."
