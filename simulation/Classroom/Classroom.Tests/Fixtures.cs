using Classroom;

internal static class Fixtures
{
    internal static ClassroomStore Store(string? path = null) => new(path,
    [
        new("room-a101", "测试教室 A101", 36),
        new("room-a102", "测试教室 A102", 48),
        new("room-b201", "测试教室 B201", 60),
        new("room-c305", "测试教室 C305", 24)
    ]);
}
