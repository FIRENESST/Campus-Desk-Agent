using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Classroom;

public sealed record ClassroomRoom(string RoomId, string Name, int Capacity);
public sealed record ClassroomReservation(
    string ReservationId, string RoomId, string UserName,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] DateTimeOffset? StartsAt = null,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] DateTimeOffset? EndsAt = null,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? RoomName = null);
public sealed record StoreError(string Code, string Message);
public sealed record StoreResult<T>(T? Value, StoreError? Error)
{
    public static StoreResult<T> Success(T value) => new(value, null);
}

public sealed class ClassroomStore
{
    private readonly ClassroomRoom[] _rooms;

    private readonly string? _storagePath;
    private List<ClassroomReservation> _reservations;
    private readonly object _sync = new();

    public ClassroomStore(string? storagePath = null, IEnumerable<ClassroomRoom>? rooms = null)
    {
        _rooms = rooms?.ToArray() ?? [];
        if (_rooms.Any(room => room is null || string.IsNullOrWhiteSpace(room.RoomId)
            || room.RoomId.Length > 64 || string.IsNullOrWhiteSpace(room.Name) || room.Capacity < 1)
            || _rooms.Select(room => room.RoomId).Distinct(StringComparer.Ordinal).Count() != _rooms.Length)
            throw new InvalidDataException("教室清单存在无效条目或重复 ID。");
        _storagePath = storagePath;
        _reservations = storagePath is not null && File.Exists(storagePath)
            ? JsonSerializer.Deserialize<List<ClassroomReservation>>(File.ReadAllBytes(storagePath))
                ?? throw new InvalidDataException("教室预约文件无效。")
            : [];
    }

    public static ClassroomRoom[] LoadRooms(string? file)
    {
        if (string.IsNullOrWhiteSpace(file)) return [];
        using var stream = File.OpenRead(file);
        if (stream.Length > 8 * 1024 * 1024) throw new InvalidDataException("教室清单过大。");
        return JsonSerializer.Deserialize<ClassroomRoom[]>(stream, new JsonSerializerOptions(JsonSerializerDefaults.Web))
            ?? throw new InvalidDataException("教室清单须为 JSON 数组。");
    }

    public IReadOnlyList<ClassroomRoom> ListRooms() => _rooms;

    public IReadOnlyList<ClassroomReservation> ListReservations(
        DateTimeOffset? startsBefore = null, DateTimeOffset? endsAfter = null)
    {
        lock (_sync) return _reservations.Where(item =>
            (startsBefore is null || item.StartsAt is null || item.StartsAt < startsBefore)
            && (endsAfter is null || item.EndsAt is null || item.EndsAt > endsAfter)).ToArray();
    }

    public StoreResult<ClassroomReservation> ReserveRoom(
        string? roomId, string? userName, string? startsAt = null, string? endsAt = null)
    {
        if (string.IsNullOrWhiteSpace(roomId) || string.IsNullOrWhiteSpace(userName))
            return new(null, new StoreError("INVALID_ARGUMENT", "room_id 和 user_name 不能为空。"));

        DateTimeOffset? start = null, end = null;
        if (startsAt is not null || endsAt is not null)
        {
            if (!TryReadTime(startsAt, out var parsedStart) || !TryReadTime(endsAt, out var parsedEnd)
                || parsedEnd <= parsedStart)
                return new(null, new StoreError("INVALID_ARGUMENT", "请填写有效的起止时间，结束时间须晚于开始时间。"));
            start = parsedStart;
            end = parsedEnd;
        }

        lock (_sync)
        {
            roomId = roomId.Trim();
            userName = userName.Trim();
            var room = _rooms.SingleOrDefault(room => room.RoomId == roomId);
            if (room is null)
                return new(null, new StoreError("NOT_FOUND", $"教室 {roomId} 不存在。"));
            var now = DateTimeOffset.UtcNow;
            if (start <= now)
                return new(null, new StoreError("SLOT_UNAVAILABLE", "该教室预约时段已开始。"));
            if (_reservations.Any(item => item.RoomId == roomId && (item.EndsAt is null
                || (start is null ? item.EndsAt > now : start < item.EndsAt && item.StartsAt < end))))
                return new(null, new StoreError("CONFLICT", $"教室 {roomId} 在所选时段已有预约。"));

            var reservation = new ClassroomReservation(
                $"res-{Guid.NewGuid():N}",
                roomId,
                userName, start, end, start is not null ? room.Name : null);
            Save([.. _reservations, reservation]);
            return StoreResult<ClassroomReservation>.Success(reservation);
        }
    }

    public StoreResult<ClassroomReservation> CancelReservation(string? reservationId)
    {
        if (string.IsNullOrWhiteSpace(reservationId))
            return new(null, new StoreError("INVALID_ARGUMENT", "reservation_id 不能为空。"));

        lock (_sync)
        {
            reservationId = reservationId.Trim();
            var reservation = _reservations.SingleOrDefault(item => item.ReservationId == reservationId);
            if (reservation is null)
                return new(null, new StoreError("NOT_FOUND", $"预约 {reservationId} 不存在。"));

            Save(_reservations.Where(item => item.ReservationId != reservationId).ToList());
            return StoreResult<ClassroomReservation>.Success(reservation);
        }
    }

    internal static bool TryReadTime(string? value, out DateTimeOffset time) =>
        DateTimeOffset.TryParseExact(value?.Replace("Z", "+00:00", StringComparison.Ordinal),
            ["yyyy-MM-dd'T'HH:mmzzz", "yyyy-MM-dd'T'HH:mm:sszzz"],
            CultureInfo.InvariantCulture, DateTimeStyles.None, out time) && time.Second == 0;

    private void Save(List<ClassroomReservation> reservations)
    {
        if (_storagePath is not null)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(_storagePath))!);
            var temporary = _storagePath + ".tmp";
            using (var file = File.Create(temporary))
            {
                JsonSerializer.Serialize(file, reservations);
                file.Flush(flushToDisk: true);
            }
            File.Move(temporary, _storagePath, overwrite: true);
        }
        _reservations = reservations;
    }
}
