using System.Text.Json;
using Classroom;

static class ProtocolTests
{
    public static (string Name, Action Run)[] Cases() =>
    [
        ("Agent list_tools follows the exact tool contract", () =>
        {
            using var response = Handle(Fixtures.Store(), """{"id":"tools-1","type":"list_tools"}""");
            var root = response.RootElement;

            Assert.Equal("tools-1", root.GetProperty("id").GetString());
            Assert.Equal("tools", root.GetProperty("type").GetString());
            var tools = root.GetProperty("tools");
            Assert.Equal(3, tools.GetArrayLength());
            Assert.Equal("classroom_list_rooms,classroom_reserve_room,classroom_cancel_reservation",
                string.Join(',', tools.EnumerateArray().Select(tool => tool.GetProperty("name").GetString())));
            foreach (var tool in tools.EnumerateArray())
            {
                Assert.True(!string.IsNullOrWhiteSpace(tool.GetProperty("description").GetString()), "工具描述不能为空");
                Assert.Equal(JsonValueKind.Object, tool.GetProperty("parameters").ValueKind);
            }
        }),
        ("Agent call_tool returns string content and shares the store", () =>
        {
            var store = Fixtures.Store();
            using var reserved = Handle(store, """
                {"id":"agent-reserve","type":"call_tool","name":"classroom_reserve_room","arguments":{"room_id":"room-a101","user_name":"协议用户"}}
                """);
            var root = reserved.RootElement;
            Assert.Equal("agent-reserve", root.GetProperty("id").GetString());
            Assert.Equal("tool_result", root.GetProperty("type").GetString());
            Assert.Equal(false, root.GetProperty("isError").GetBoolean());
            Assert.Equal(JsonValueKind.String, root.GetProperty("content").ValueKind);

            using var content = JsonDocument.Parse(root.GetProperty("content").GetString()!);
            var reservationId = content.RootElement.GetProperty("reservation").GetProperty("reservationId").GetString();
            Assert.Equal(1, store.ListReservations().Count);

            using var cancelled = Handle(store, JsonSerializer.Serialize(new
            {
                id = "agent-cancel",
                type = "call_tool",
                name = "classroom_cancel_reservation",
                arguments = new { reservation_id = reservationId }
            }));
            Assert.Equal("agent-cancel", cancelled.RootElement.GetProperty("id").GetString());
            Assert.Equal(false, cancelled.RootElement.GetProperty("isError").GetBoolean());
            Assert.Equal(0, store.ListReservations().Count);
        }),
        ("Agent business failures use tool_result with isError true", () =>
        {
            using var response = Handle(Fixtures.Store(), """
                {"id":"agent-error","type":"call_tool","name":"classroom_reserve_room","arguments":{"room_id":"room-unknown","user_name":"张三"}}
                """);
            var root = response.RootElement;

            Assert.Equal("agent-error", root.GetProperty("id").GetString());
            Assert.Equal("tool_result", root.GetProperty("type").GetString());
            Assert.Equal(true, root.GetProperty("isError").GetBoolean());
            Assert.Equal(JsonValueKind.String, root.GetProperty("content").ValueKind);
            using var content = JsonDocument.Parse(root.GetProperty("content").GetString()!);
            Assert.Equal("NOT_FOUND", content.RootElement.GetProperty("error").GetProperty("code").GetString());
        }),
        ("Agent missing tool name is an invalid argument", () =>
        {
            using var response = Handle(Fixtures.Store(), """
                {"id":"agent-missing-name","type":"call_tool","arguments":{}}
                """);
            var root = response.RootElement;

            Assert.Equal("agent-missing-name", root.GetProperty("id").GetString());
            Assert.Equal("tool_result", root.GetProperty("type").GetString());
            Assert.Equal(true, root.GetProperty("isError").GetBoolean());
            using var content = JsonDocument.Parse(root.GetProperty("content").GetString()!);
            Assert.Equal("INVALID_ARGUMENT", content.RootElement.GetProperty("error").GetProperty("code").GetString());
        }),
        ("Domain messages support the complete reservation flow", () =>
        {
            var store = Fixtures.Store();
            using var rooms = Handle(store, """{"id":"domain-rooms","type":"list_rooms"}""");
            Assert.Equal("domain-rooms", rooms.RootElement.GetProperty("id").GetString());
            Assert.Equal("rooms", rooms.RootElement.GetProperty("type").GetString());
            Assert.Equal(4, rooms.RootElement.GetProperty("rooms").GetArrayLength());

            using var reserved = Handle(store, """{"id":"domain-reserve","type":"reserve_room","room_id":"room-a101","user_name":"前端用户"}""");
            Assert.Equal("reservation_created", reserved.RootElement.GetProperty("type").GetString());
            var reservationId = reserved.RootElement.GetProperty("reservation").GetProperty("reservationId").GetString();

            using var current = Handle(store, """{"id":"domain-current","type":"list_reservations"}""");
            Assert.Equal(1, current.RootElement.GetProperty("reservations").GetArrayLength());

            using var cancelled = Handle(store, JsonSerializer.Serialize(new
            {
                id = "domain-cancel",
                type = "cancel_reservation",
                reservation_id = reservationId
            }));
            Assert.Equal("domain-cancel", cancelled.RootElement.GetProperty("id").GetString());
            Assert.Equal("reservation_cancelled", cancelled.RootElement.GetProperty("type").GetString());
            Assert.Equal(0, store.ListReservations().Count);
        }),
        ("Protocol errors are stable and echo the request id", () =>
        {
            var store = Fixtures.Store();
            using var conflictSetup = Handle(store, """{"id":"setup","type":"reserve_room","room_id":"room-a101","user_name":"张三"}""");
            using var conflict = Handle(store, """{"id":"conflict","type":"reserve_room","room_id":"room-a101","user_name":"李四"}""");
            Assert.Equal("conflict", conflict.RootElement.GetProperty("id").GetString());
            Assert.Equal("error", conflict.RootElement.GetProperty("type").GetString());
            Assert.Equal("CONFLICT", conflict.RootElement.GetProperty("error").GetProperty("code").GetString());

            using var unknown = Handle(store, """{"id":"unknown-type","type":"not_a_message"}""");
            Assert.Equal("unknown-type", unknown.RootElement.GetProperty("id").GetString());
            Assert.Equal("error", unknown.RootElement.GetProperty("type").GetString());
            Assert.Equal("PROTOCOL_ERROR", unknown.RootElement.GetProperty("error").GetProperty("code").GetString());

            using var stillWorks = Handle(store, """{"id":"after-error","type":"list_rooms"}""");
            Assert.Equal("after-error", stillWorks.RootElement.GetProperty("id").GetString());
            Assert.Equal("rooms", stillWorks.RootElement.GetProperty("type").GetString());
        })
    ];

    private static JsonDocument Handle(ClassroomStore store, string json) =>
        JsonDocument.Parse(ClassroomProtocol.Handle(store, json));
}
