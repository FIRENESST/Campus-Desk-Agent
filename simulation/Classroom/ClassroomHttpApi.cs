using System.Text.Json;

namespace Classroom;

public sealed record HttpApiResponse(int StatusCode, string Body);

public static class ClassroomHttpApi
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase
    };

    public static HttpApiResponse Health() =>
        new(200, Serialize(new { status = "ok", service = "classroom" }));

    public static HttpApiResponse Reservations(ClassroomStore store, string? startsBefore = null, string? endsAfter = null)
    {
        DateTimeOffset? before = null, after = null;
        if (startsBefore is not null)
        {
            if (!ClassroomStore.TryReadTime(startsBefore, out var value)) return InvalidRequest();
            before = value;
        }
        if (endsAfter is not null)
        {
            if (!ClassroomStore.TryReadTime(endsAfter, out var value)) return InvalidRequest();
            after = value;
        }
        return new(200, Serialize(new { reservations = store.ListReservations(before, after) }));
    }

    public static HttpApiResponse Availability(ClassroomStore store)
    {
        var now = DateTimeOffset.UtcNow;
        var reserved = store.ListReservations()
            .Where(item => item.StartsAt is null || item.StartsAt <= now && item.EndsAt > now)
            .Select(item => item.RoomId).ToHashSet(StringComparer.Ordinal);
        var rooms = store.ListRooms().Select(room => new
        {
            room.RoomId,
            room.Name,
            room.Capacity,
            reserved = reserved.Contains(room.RoomId)
        });
        return new HttpApiResponse(200, Serialize(new { rooms }));
    }

    public static HttpApiResponse Tools()
    {
        using var protocol = JsonDocument.Parse(ClassroomProtocol.ListTools(null));
        return new HttpApiResponse(200, Serialize(new
        {
            tools = protocol.RootElement.GetProperty("tools").Clone()
        }));
    }

    public static HttpApiResponse Call(ClassroomStore store, string json)
    {
        try
        {
            using var request = JsonDocument.Parse(json);
            var root = request.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || root.EnumerateObject().Count() != 2
                || !root.TryGetProperty("name", out var nameValue)
                || nameValue.ValueKind != JsonValueKind.String
                || !root.TryGetProperty("arguments", out var arguments)
                || arguments.ValueKind != JsonValueKind.Object)
            {
                return InvalidRequest();
            }

            var name = nameValue.GetString()!;
            if (!IsKnownTool(name))
                return InvokeProtocol(store, name, arguments);
            if (!HasValidArguments(name, arguments))
                return InvalidRequest();

            return InvokeProtocol(store, name, arguments);
        }
        catch (JsonException)
        {
            return InvalidRequest();
        }
    }

    private static HttpApiResponse InvokeProtocol(ClassroomStore store, string name, JsonElement arguments)
    {
        var request = Serialize(new
        {
            type = "call_tool",
            name,
            arguments = arguments.Clone()
        });
        using var response = JsonDocument.Parse(ClassroomProtocol.Handle(store, request));
        var content = response.RootElement.GetProperty("content").GetString()!;
        if (!response.RootElement.GetProperty("isError").GetBoolean())
            return new HttpApiResponse(200, Serialize(new { content }));

        using var contentDocument = JsonDocument.Parse(content);
        var error = contentDocument.RootElement.GetProperty("error");
        var code = error.GetProperty("code").GetString()!;
        var message = error.GetProperty("message").GetString()!;
        return Error(StatusFor(code), code, message);
    }

    private static bool IsKnownTool(string name) => name is
        "classroom_list_rooms" or "classroom_reserve_room" or "classroom_cancel_reservation";

    private static bool HasValidArguments(string name, JsonElement arguments) => name switch
    {
        "classroom_list_rooms" => !arguments.EnumerateObject().Any(),
        "classroom_reserve_room" => HasExactStringProperties(arguments, "room_id", "user_name")
            || HasExactStringProperties(arguments, "room_id", "user_name", "starts_at", "ends_at"),
        "classroom_cancel_reservation" => HasExactStringProperties(arguments, "reservation_id"),
        _ => false
    };

    private static bool HasExactStringProperties(JsonElement arguments, params string[] names)
    {
        if (arguments.EnumerateObject().Count() != names.Length)
            return false;
        return names.All(name => arguments.TryGetProperty(name, out var value)
            && value.ValueKind == JsonValueKind.String);
    }

    private static HttpApiResponse InvalidRequest() =>
        Error(400, "INVALID_ARGUMENT", "请求必须且只能包含字符串 name 和对象 arguments，并符合工具参数 Schema。");

    private static int StatusFor(string code) => code switch
    {
        "NOT_FOUND" => 404,
        "CONFLICT" or "CAPACITY_FULL" or "SLOT_UNAVAILABLE" => 409,
        _ => 400
    };

    private static HttpApiResponse Error(int status, string code, string message) =>
        new(status, Serialize(new { error = new { code, message, retryable = false } }));

    private static string Serialize(object value) => JsonSerializer.Serialize(value, JsonOptions);
}
