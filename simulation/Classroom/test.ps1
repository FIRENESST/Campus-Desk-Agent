$ErrorActionPreference = "Stop"
dotnet run --project "$PSScriptRoot\Classroom.Tests\Classroom.Tests.csproj" --no-launch-profile
exit $LASTEXITCODE
