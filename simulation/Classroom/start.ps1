$ErrorActionPreference = "Stop"
dotnet run --project "$PSScriptRoot\Classroom.csproj" --no-launch-profile
exit $LASTEXITCODE
