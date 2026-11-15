using System.Net.WebSockets;
using System.Text;
using Classroom;

var builder = WebApplication.CreateBuilder(args);
builder.WebHost.UseUrls(Environment.GetEnvironmentVariable("CLASSROOM_URLS") ?? "http://127.0.0.1:19004");

var app = builder.Build();
var rooms = ClassroomStore.LoadRooms(Environment.GetEnvironmentVariable("CLASSROOM_ROOMS_FILE"));
var store = new ClassroomStore(Path.Combine(app.Environment.ContentRootPath, ".data", "reservations.json"), rooms);

app.UseDefaultFiles();
app.UseStaticFiles();
app.UseWebSockets();

app.MapGet("/healthz", () => Respond(ClassroomHttpApi.Health()));
app.MapGet("/admin", (IWebHostEnvironment environment) =>
    Results.File(
        Path.Combine(environment.WebRootPath, "admin.html"),
        "text/html; charset=utf-8"));
app.MapGet("/api/availability", () => Respond(ClassroomHttpApi.Availability(store)));
app.MapGet("/api/reservations", (string? starts_before, string? ends_after) =>
    Respond(ClassroomHttpApi.Reservations(store, starts_before, ends_after)));
app.MapGet("/agent/tools", () => Respond(ClassroomHttpApi.Tools()));
app.MapPost("/agent/call", async (HttpContext context) =>
{
    using var reader = new StreamReader(context.Request.Body, Encoding.UTF8);
    var request = await reader.ReadToEndAsync(context.RequestAborted);
    return Respond(ClassroomHttpApi.Call(store, request));
});

app.Map("/ws", async context =>
{
    if (!context.WebSockets.IsWebSocketRequest)
    {
        context.Response.StatusCode = StatusCodes.Status400BadRequest;
        return;
    }

    using var socket = await context.WebSockets.AcceptWebSocketAsync();
    var buffer = new byte[8 * 1024];

    while (socket.State == WebSocketState.Open)
    {
        using var message = new MemoryStream();
        WebSocketReceiveResult received;
        do
        {
            received = await socket.ReceiveAsync(buffer, context.RequestAborted);
            if (received.MessageType == WebSocketMessageType.Close)
            {
                await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "关闭连接", CancellationToken.None);
                return;
            }
            message.Write(buffer, 0, received.Count);
        } while (!received.EndOfMessage);

        var request = received.MessageType == WebSocketMessageType.Text
            ? Encoding.UTF8.GetString(message.ToArray())
            : "null";
        var response = ClassroomProtocol.Handle(store, request);
        await socket.SendAsync(
            Encoding.UTF8.GetBytes(response),
            WebSocketMessageType.Text,
            true,
            context.RequestAborted);
    }
});

app.Run();

static IResult Respond(HttpApiResponse response) =>
    Results.Text(response.Body, "application/json", Encoding.UTF8, response.StatusCode);
