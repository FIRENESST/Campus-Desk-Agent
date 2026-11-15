using System.Text.Json;

namespace Classroom;

public static class ClassroomProtocol
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase
    };

    public static string Handle(ClassroomStore store, string json)
    {
        string? id = null;
        try
        {
            using var document = JsonDocument.Parse(json);
            var request = document.RootElement;
            if (request.ValueKind != JsonValueKind.Object)
                return ProtocolError(null, "消息必须是 JSON 对象。");

            id = StringProperty(request, "id");
            var type = StringProperty(request, "type");
            return type switch
            {
                "list_tools" => ListTools(id),
                "call_tool" => CallTool(store, id, request),
                "list_rooms" => Serialize(new { id, type = "rooms", rooms = store.ListRooms() }),
                "list_reservations" => Serialize(new { id, type = "reservations", reservations = store.ListReservations() }),
                "reserve_room" => ReserveRoom(store, id, request),
                "cancel_reservation" => CancelReservation(store, id, request),
                _ => ProtocolError(id, $"未知消息类型：{type ?? "空"}。")
            };
        }
        catch (JsonException)
        {
            return ProtocolError(id, "消息必须是有效的 JSON。");
        }
    }

    private static string ReserveRoom(ClassroomStore store, string? id, JsonElement request)
    {
        var result = store.ReserveRoom(
            StringProperty(request, "room_id"),
            StringProperty(request, "user_name"),
            TimeProperty(request, "starts_at"),
            TimeProperty(request, "ends_at"));
        return result.Error is null
            ? Serialize(new { id, type = "reservation_created", reservation = result.Value })
            : Error(id, result.Error);
    }

    private static string CancelReservation(ClassroomStore store, string? id, JsonElement request)
    {
        var result = store.CancelReservation(StringProperty(request, "reservation_id"));
        return result.Error is null
            ? Serialize(new { id, type = "reservation_cancelled", reservation = result.Value })
            : Error(id, result.Error);
    }

    private static string CallTool(ClassroomStore store, string? id, JsonElement request)
    {
        var name = StringProperty(request, "name");
        if (string.IsNullOrWhiteSpace(name))
            return ToolResult(id,
                new { error = new StoreError("INVALID_ARGUMENT", "name 不能为空。") }, true);

        switch (name)
        {
            case "classroom_list_rooms":
                return ToolResult(id, new { rooms = store.ListRooms() }, false);
            case "classroom_reserve_room":
            {
                var result = request.TryGetProperty("arguments", out var args) && args.ValueKind == JsonValueKind.Object
                    ? store.ReserveRoom(StringProperty(args, "room_id"), StringProperty(args, "user_name"),
                        TimeProperty(args, "starts_at"), TimeProperty(args, "ends_at"))
                    : store.ReserveRoom(null, null);
                return result.Error is null
                    ? ToolResult(id, new { reservation = result.Value }, false)
                    : ToolResult(id, new { error = result.Error }, true);
            }
            case "classroom_cancel_reservation":
            {
                var result = store.CancelReservation(ArgumentProperty(request, "reservation_id"));
                return result.Error is null
                    ? ToolResult(id, new { reservation = result.Value }, false)
                    : ToolResult(id, new { error = result.Error }, true);
            }
            default:
                return ToolResult(id,
                    new { error = new StoreError("NOT_FOUND", $"工具 {name ?? "空"} 不存在。") }, true);
        }
    }

    internal static string ListTools(string? id) => Serialize(new
    {
        id,
        type = "tools",
        tools = new object[]
        {
            new
            {
                name = "classroom_list_rooms",
                description = "查询全部教室及容量。",
                parameters = new { type = "object", properties = new { }, additionalProperties = false }
            },
            new
            {
                name = "classroom_reserve_room",
                description = "创建教室预约。同一教室的时段不可重叠。新区间请同时提供开始与结束时间。",
                parameters = new
                {
                    type = "object",
                    properties = new
                    {
                        room_id = new { type = "string", description = "教室 ID" },
                        user_name = new { type = "string", description = "预约人标识" },
                        starts_at = new { type = "string", description = "开始时间，ISO 8601 格式，含时区，精确到分钟" },
                        ends_at = new { type = "string", description = "结束时间，ISO 8601 格式，含时区，精确到分钟" }
                    },
                    required = new[] { "room_id", "user_name" },
                    additionalProperties = false
                }
            },
            new
            {
                name = "classroom_cancel_reservation",
                description = "按预约 ID 取消活跃预约。",
                parameters = new
                {
                    type = "object",
                    properties = new
                    {
                        reservation_id = new { type = "string", description = "预约 ID" }
                    },
                    required = new[] { "reservation_id" },
                    additionalProperties = false
                }
            }
        }
    });

    private static string ToolResult(string? id, object payload, bool isError) => Serialize(new
    {
        id,
        type = "tool_result",
        content = Serialize(payload),
        isError
    });

    private static string Error(string? id, StoreError error) => Serialize(new { id, type = "error", error });

    private static string ProtocolError(string? id, string message) =>
        Error(id, new StoreError("PROTOCOL_ERROR", message));

    private static string? StringProperty(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;

    private static string? ArgumentProperty(JsonElement request, string name) =>
        request.TryGetProperty("arguments", out var arguments) && arguments.ValueKind == JsonValueKind.Object
            ? StringProperty(arguments, name)
            : null;

    private static string? TimeProperty(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) ? (value.ValueKind == JsonValueKind.String ? value.GetString() : "") : null;

    private static string Serialize(object value) => JsonSerializer.Serialize(value, JsonOptions);
}
