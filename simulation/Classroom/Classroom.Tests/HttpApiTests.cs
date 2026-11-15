using System.Text.Json;
using Classroom;

static class HttpApiTests
{
    public static (string Name, Action Run)[] Cases() =>
    [
        ("HTTP health returns the exact classroom identity", () =>
        {
            using var response = Parse(ClassroomHttpApi.Health(), 200);

            Assert.Equal(JsonValueKind.Object, response.RootElement.ValueKind);
            Assert.Equal("service,status", string.Join(',', response.RootElement.EnumerateObject().Select(property => property.Name).OrderBy(name => name)));
            Assert.Equal("ok", response.RootElement.GetProperty("status").GetString());
            Assert.Equal("classroom", response.RootElement.GetProperty("service").GetString());
        }),
        ("HTTP tools reuse the WebSocket tool definitions and schemas", () =>
        {
            using var response = Parse(ClassroomHttpApi.Tools(), 200);
            var tools = response.RootElement.GetProperty("tools");
            Assert.Equal(3, tools.GetArrayLength());
            Assert.Equal("tools", response.RootElement.EnumerateObject().Single().Name);

            using var protocol = JsonDocument.Parse(ClassroomProtocol.Handle(Fixtures.Store(), """{"type":"list_tools"}"""));
            var protocolTools = protocol.RootElement.GetProperty("tools").EnumerateArray().ToArray();
            foreach (var tool in tools.EnumerateArray())
            {
                Assert.Equal("description,name,parameters", string.Join(',', tool.EnumerateObject().Select(property => property.Name).OrderBy(name => name)));
                var expected = protocolTools.Single(item => item.GetProperty("name").GetString() == tool.GetProperty("name").GetString());
                Assert.Equal(expected.GetRawText(), tool.GetRawText());
                Assert.Equal(JsonValueKind.Object, tool.GetProperty("parameters").ValueKind);
                Assert.Equal(false, tool.GetProperty("parameters").GetProperty("additionalProperties").GetBoolean());
            }
        }),
        ("HTTP agent calls return JSON text and share state with WebSocket", () =>
        {
            var store = Fixtures.Store();
            using var rooms = Parse(ClassroomHttpApi.Call(store, """{"name":"classroom_list_rooms","arguments":{}}"""), 200);
            using var roomsContent = JsonDocument.Parse(rooms.RootElement.GetProperty("content").GetString()!);
            Assert.Equal(4, roomsContent.RootElement.GetProperty("rooms").GetArrayLength());
            Assert.Equal("room-a101", roomsContent.RootElement.GetProperty("rooms")[0].GetProperty("roomId").GetString());

            using var reserved = Parse(ClassroomHttpApi.Call(store, """
                {"name":"classroom_reserve_room","arguments":{"room_id":"room-a101","user_name":"HTTP 用户"}}
                """), 200);
            Assert.Equal("content", reserved.RootElement.EnumerateObject().Single().Name);
            Assert.Equal(JsonValueKind.String, reserved.RootElement.GetProperty("content").ValueKind);

            using var content = JsonDocument.Parse(reserved.RootElement.GetProperty("content").GetString()!);
            var reservation = content.RootElement.GetProperty("reservation");
            var reservationId = reservation.GetProperty("reservationId").GetString();
            Assert.Equal("room-a101", reservation.GetProperty("roomId").GetString());
            Assert.Equal("HTTP 用户", reservation.GetProperty("userName").GetString());
            Assert.Equal(1, store.ListReservations().Count);

            using var socketView = JsonDocument.Parse(ClassroomProtocol.Handle(store, """{"type":"list_reservations"}"""));
            Assert.Equal(reservationId, socketView.RootElement.GetProperty("reservations")[0].GetProperty("reservationId").GetString());

            using var availability = Parse(ClassroomHttpApi.Availability(store), 200);
            var occupied = availability.RootElement.GetProperty("rooms")[0];
            Assert.Equal("capacity,name,reserved,roomId", string.Join(',', occupied.EnumerateObject().Select(property => property.Name).OrderBy(name => name)));
            Assert.Equal("room-a101", occupied.GetProperty("roomId").GetString());
            Assert.Equal(true, occupied.GetProperty("reserved").GetBoolean());

            using var cancelled = Parse(ClassroomHttpApi.Call(store, JsonSerializer.Serialize(new
            {
                name = "classroom_cancel_reservation",
                arguments = new { reservation_id = reservationId }
            })), 200);
            using var cancelledContent = JsonDocument.Parse(cancelled.RootElement.GetProperty("content").GetString()!);
            Assert.Equal(reservationId, cancelledContent.RootElement.GetProperty("reservation").GetProperty("reservationId").GetString());
            Assert.Equal(0, store.ListReservations().Count);
            using var freed = Parse(ClassroomHttpApi.Availability(store), 200);
            Assert.Equal(false, freed.RootElement.GetProperty("rooms")[0].GetProperty("reserved").GetBoolean());
        }),
        ("HTTP agent rejects malformed envelopes and non-object arguments", () =>
        {
            var store = Fixtures.Store();
            var requests = new[]
            {
                "{broken",
                "[]",
                "null",
                "{}",
                "{\"name\":\"classroom_list_rooms\"}",
                "{\"arguments\":{}}",
                "{\"name\":1,\"arguments\":{}}",
                "{\"name\":\"classroom_list_rooms\",\"arguments\":null}",
                "{\"name\":\"classroom_list_rooms\",\"arguments\":[]}",
                "{\"name\":\"classroom_list_rooms\",\"arguments\":\"not-an-object\"}",
                "{\"name\":\"classroom_list_rooms\",\"arguments\":{},\"extra\":true}"
            };

            foreach (var request in requests)
                AssertError(ClassroomHttpApi.Call(store, request), 400, "INVALID_ARGUMENT");
        }),
        ("HTTP agent strictly validates each tool argument schema", () =>
        {
            var store = Fixtures.Store();
            var requests = new[]
            {
                "{\"name\":\"classroom_list_rooms\",\"arguments\":{\"extra\":1}}",
                "{\"name\":\"classroom_reserve_room\",\"arguments\":{\"room_id\":\"room-a101\"}}",
                "{\"name\":\"classroom_reserve_room\",\"arguments\":{\"room_id\":\"room-a101\",\"user_name\":\"张三\",\"extra\":1}}",
                "{\"name\":\"classroom_reserve_room\",\"arguments\":{\"room_id\":1,\"user_name\":\"张三\"}}",
                "{\"name\":\"classroom_cancel_reservation\",\"arguments\":{}}",
                "{\"name\":\"classroom_cancel_reservation\",\"arguments\":{\"reservation_id\":\"res-1\",\"extra\":1}}",
                "{\"name\":\"classroom_cancel_reservation\",\"arguments\":{\"reservation_id\":false}}"
            };

            foreach (var request in requests)
                AssertError(ClassroomHttpApi.Call(store, request), 400, "INVALID_ARGUMENT");
            Assert.Equal(0, store.ListReservations().Count);
        }),
        ("Timed reservations allow adjacent slots and survive restart until cancelled", () =>
        {
            var directory = Directory.CreateTempSubdirectory("classroom-test-");
            var path = Path.Combine(directory.FullName, "reservations.json");
            var store = Fixtures.Store(path);
            var first = DateTimeOffset.UtcNow.AddDays(2).Date.AddHours(2);
            var start = new DateTimeOffset(first, TimeSpan.Zero);
            var middle = start.AddHours(1);
            var end = middle.AddHours(1);
            static string Stamp(DateTimeOffset value) => value.ToString("yyyy-MM-dd'T'HH:mmzzz");
            HttpApiResponse Reserve(DateTimeOffset from, DateTimeOffset until) => ClassroomHttpApi.Call(store,
                JsonSerializer.Serialize(new { name = "classroom_reserve_room", arguments = new {
                    room_id = "room-a101", user_name = "测试", starts_at = Stamp(from), ends_at = Stamp(until),
                } }));

            try
            {
                Assert.Equal(200, Reserve(start, middle).StatusCode);
                AssertError(Reserve(start.AddMinutes(30), end), 409, "CONFLICT");
                AssertError(Reserve(end, middle), 400, "INVALID_ARGUMENT");
                Assert.Equal(200, Reserve(middle, end).StatusCode);
                using var slot = Parse(ClassroomHttpApi.Reservations(store, Stamp(end), Stamp(middle)), 200);
                var receipt = slot.RootElement.GetProperty("reservations").EnumerateArray().Single();
                Assert.Equal(middle, receipt.GetProperty("startsAt").GetDateTimeOffset());
                Assert.Equal(end, receipt.GetProperty("endsAt").GetDateTimeOffset());
                Assert.Equal("测试教室 A101", receipt.GetProperty("roomName").GetString());
                using var availability = Parse(ClassroomHttpApi.Availability(store), 200);
                Assert.Equal(false, availability.RootElement.GetProperty("rooms")[0].GetProperty("reserved").GetBoolean());
                var reopened = Fixtures.Store(path);
                Assert.Equal(2, reopened.ListReservations().Count);
                Assert.True(reopened.CancelReservation(receipt.GetProperty("reservationId").GetString()).Error is null,
                    "重启后能取消预约");
                Assert.Equal(1, Fixtures.Store(path).ListReservations().Count);
            }
            finally
            {
                File.Delete(path);
                directory.Delete();
            }
        }),
        ("HTTP agent maps unknown tools and business errors to stable statuses", () =>
        {
            var store = Fixtures.Store();
            AssertError(ClassroomHttpApi.Call(store, """{"name":"unknown","arguments":{}}"""), 404, "NOT_FOUND");
            AssertError(ClassroomHttpApi.Call(store, """
                {"name":"classroom_reserve_room","arguments":{"room_id":"room-unknown","user_name":"张三"}}
                """), 404, "NOT_FOUND");
            AssertError(ClassroomHttpApi.Call(store, """
                {"name":"classroom_cancel_reservation","arguments":{"reservation_id":"res-unknown"}}
                """), 404, "NOT_FOUND");

            Assert.Equal(200, ClassroomHttpApi.Call(store, """
                {"name":"classroom_reserve_room","arguments":{"room_id":"room-a101","user_name":"张三"}}
                """).StatusCode);
            AssertError(ClassroomHttpApi.Call(store, """
                {"name":"classroom_reserve_room","arguments":{"room_id":"room-a101","user_name":"李四"}}
                """), 409, "CONFLICT");
        })
    ];

    private static JsonDocument Parse(HttpApiResponse response, int status)
    {
        Assert.Equal(status, response.StatusCode);
        return JsonDocument.Parse(response.Body);
    }

    private static void AssertError(HttpApiResponse response, int status, string code)
    {
        using var document = Parse(response, status);
        var root = document.RootElement;
        Assert.Equal("error", root.EnumerateObject().Single().Name);
        var error = root.GetProperty("error");
        Assert.Equal("code,message,retryable", string.Join(',', error.EnumerateObject().Select(property => property.Name).OrderBy(name => name)));
        Assert.Equal(code, error.GetProperty("code").GetString());
        Assert.True(!string.IsNullOrWhiteSpace(error.GetProperty("message").GetString()), "错误消息不能为空");
        Assert.Equal(false, error.GetProperty("retryable").GetBoolean());
    }
}
