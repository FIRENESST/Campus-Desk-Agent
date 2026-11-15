using Classroom;

var tests = new (string Name, Action Run)[]
{
    ("ListRooms returns fixed classrooms", () =>
    {
        var store = Fixtures.Store();
        var rooms = store.ListRooms();

        Assert.Equal(4, rooms.Count);
        Assert.Equal(4, rooms.Select(room => room.RoomId).Distinct().Count());
        Assert.True(rooms.All(room => room.Capacity > 0), "教室容量必须为正数");
    }),
    ("ReserveRoom creates a visible active reservation", () =>
    {
        var store = Fixtures.Store();
        var result = store.ReserveRoom("room-a101", "  张三  ");

        Assert.True(result.Error is null, $"预约应成功：{result.Error?.Message}");
        var reservation = result.Value ?? throw new InvalidOperationException("成功结果缺少预约");
        Assert.Equal("room-a101", reservation.RoomId);
        Assert.Equal("张三", reservation.UserName);
        Assert.True(!string.IsNullOrWhiteSpace(reservation.ReservationId), "预约 ID 不能为空");

        var current = store.ListReservations();
        Assert.Equal(1, current.Count);
        Assert.Equal(reservation.ReservationId, current[0].ReservationId);
    }),
    ("ReserveRoom rejects blank arguments without writing", () =>
    {
        var store = Fixtures.Store();
        var results = new[]
        {
            store.ReserveRoom(null, "张三"),
            store.ReserveRoom("  ", "张三"),
            store.ReserveRoom("room-a101", null),
            store.ReserveRoom("room-a101", "  ")
        };

        foreach (var result in results)
            Assert.Equal("INVALID_ARGUMENT", result.Error?.Code);
        Assert.Equal(0, store.ListReservations().Count);
    }),
    ("ReserveRoom rejects an unknown room", () =>
    {
        var store = Fixtures.Store();
        var result = store.ReserveRoom("room-unknown", "张三");

        Assert.Equal("NOT_FOUND", result.Error?.Code);
        Assert.Equal(0, store.ListReservations().Count);
    }),
    ("ReserveRoom rejects duplicate and occupied rooms", () =>
    {
        var store = Fixtures.Store();
        var first = store.ReserveRoom("room-a101", "张三");
        Assert.True(first.Error is null, "首次预约应成功");

        var duplicate = store.ReserveRoom("room-a101", "张三");
        var occupied = store.ReserveRoom("room-a101", "李四");

        Assert.Equal("CONFLICT", duplicate.Error?.Code);
        Assert.Equal("CONFLICT", occupied.Error?.Code);
        Assert.Equal(1, store.ListReservations().Count);
    }),
    ("CancelReservation removes the reservation and frees the room", () =>
    {
        var store = Fixtures.Store();
        var reserved = store.ReserveRoom("room-a101", "张三").Value!;

        var cancelled = store.CancelReservation(reserved.ReservationId);

        Assert.True(cancelled.Error is null, $"取消应成功：{cancelled.Error?.Message}");
        Assert.Equal(reserved.ReservationId, cancelled.Value?.ReservationId);
        Assert.Equal(0, store.ListReservations().Count);
        Assert.True(store.ReserveRoom("room-a101", "李四").Error is null, "取消后教室应可再次预约");
    }),
    ("CancelReservation returns stable argument and lookup errors", () =>
    {
        var store = Fixtures.Store();

        Assert.Equal("INVALID_ARGUMENT", store.CancelReservation(null).Error?.Code);
        Assert.Equal("INVALID_ARGUMENT", store.CancelReservation("  ").Error?.Code);
        Assert.Equal("NOT_FOUND", store.CancelReservation("res-unknown").Error?.Code);
    }),
    ("Concurrent reservations allow only one active reservation per room", () =>
    {
        for (var round = 0; round < 50; round++)
        {
            var store = Fixtures.Store();
            using var gate = new ManualResetEventSlim(false);
            var tasks = Enumerable.Range(0, 32).Select(index => Task.Run(() =>
            {
                gate.Wait();
                return store.ReserveRoom("room-a101", $"用户{index}");
            })).ToArray();

            gate.Set();
            Task.WaitAll(tasks);
            var results = tasks.Select(task => task.Result).ToArray();

            Assert.Equal(1, results.Count(result => result.Error is null));
            Assert.Equal(31, results.Count(result => result.Error?.Code == "CONFLICT"));
            Assert.Equal(1, store.ListReservations().Count);
        }
    })
}.Concat(ProtocolTests.Cases()).Concat(HttpApiTests.Cases()).ToArray();

var failures = 0;
foreach (var test in tests)
{
    try
    {
        test.Run();
        Console.WriteLine($"PASS {test.Name}");
    }
    catch (Exception exception)
    {
        failures++;
        Console.Error.WriteLine($"FAIL {test.Name}: {exception.Message}");
    }
}

Console.WriteLine($"Executed {tests.Length} tests: {tests.Length - failures} passed, {failures} failed.");
return failures == 0 ? 0 : 1;

static class Assert
{
    public static void True(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    public static void Equal<T>(T expected, T actual)
    {
        if (!EqualityComparer<T>.Default.Equals(expected, actual))
            throw new InvalidOperationException($"Expected {expected}, got {actual}.");
    }
}
