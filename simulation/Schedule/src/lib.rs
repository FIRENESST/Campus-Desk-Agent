use std::{collections::HashMap, sync::Arc};

use axum::{
    body::to_bytes,
    extract::{rejection::QueryRejection, Path, Query, Request, State},
    http::StatusCode,
    response::{Html, IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

const MAX_BODY_SIZE: usize = 64 * 1024;
const DAYS: [&str; 7] = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

#[derive(Serialize)]
struct PeriodTime {
    period: u8,
    start_time: &'static str,
    end_time: &'static str,
}

impl PeriodTime {
    const fn new(period: u8, start_time: &'static str, end_time: &'static str) -> Self {
        Self {
            period,
            start_time,
            end_time,
        }
    }
}

// 示例排课时段，随课表返回给不同视图。
const PERIODS: [PeriodTime; 8] = [
    PeriodTime::new(1, "08:00", "08:45"),
    PeriodTime::new(2, "08:55", "09:40"),
    PeriodTime::new(3, "10:00", "10:45"),
    PeriodTime::new(4, "10:55", "11:40"),
    PeriodTime::new(5, "14:00", "14:45"),
    PeriodTime::new(6, "14:55", "15:40"),
    PeriodTime::new(7, "16:00", "16:45"),
    PeriodTime::new(8, "16:55", "17:40"),
];

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Course {
    course_id: String,
    course_name: String,
    #[serde(default)]
    points: u8,
    teacher: String,
    #[serde(skip_serializing)]
    staff_number: String,
    classroom: String,
    day: String,
    start_period: u8,
    end_period: u8,
    weeks: Vec<u8>,
}

fn course_points(course_id: &str) -> u8 {
    const FNV_OFFSET: u64 = 0xcbf29ce484222325;
    const FNV_PRIME: u64 = 0x100000001b3;
    let hash = course_id.bytes().fold(FNV_OFFSET, |hash, byte| {
        (hash ^ u64::from(byte)).wrapping_mul(FNV_PRIME)
    });
    1 + (hash.count_ones() % 4) as u8
}

struct Store {
    courses: HashMap<String, Vec<Course>>,
}

#[derive(Clone)]
struct AppState {
    store: Arc<Store>,
}

#[derive(Serialize)]
struct ScheduleResponse {
    #[serde(skip_serializing_if = "Option::is_none")]
    student_id: Option<String>,
    courses: Vec<Course>,
    periods: &'static [PeriodTime],
}

struct ApiError {
    status: StatusCode,
    code: &'static str,
    message: &'static str,
}

impl ApiError {
    fn invalid(message: &'static str) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            code: "INVALID_ARGUMENT",
            message,
        }
    }

    fn not_found(message: &'static str) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            code: "NOT_FOUND",
            message,
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "retryable": false
                }
            })),
        )
            .into_response()
    }
}

pub fn app() -> Router {
    app_with_store(HashMap::new())
}

/// Load an operator-supplied catalog. The default router contains no student or course records.
pub fn app_with_data(json: &[u8]) -> Result<Router, Box<dyn std::error::Error>> {
    use std::io::{Error, ErrorKind};
    if json.len() > 8 * 1024 * 1024 {
        return Err(Error::new(ErrorKind::InvalidData, "课表文件过大").into());
    }
    let mut courses: HashMap<String, Vec<Course>> = serde_json::from_slice(json)?;
    for (student, schedule) in &mut courses {
        if student.trim().is_empty() || student.len() > 64 {
            return Err(Error::new(ErrorKind::InvalidData, "学号无效").into());
        }
        for course in schedule {
            if course.course_id.trim().is_empty()
                || course.course_name.trim().is_empty()
                || course.staff_number.trim().is_empty()
                || !DAYS.contains(&course.day.as_str())
                || course.start_period < 1
                || course.end_period > PERIODS.len() as u8
                || course.start_period > course.end_period
                || course.weeks.is_empty()
                || course.weeks.iter().any(|week| !(1..=20).contains(week))
                || course.points > 4
            {
                return Err(Error::new(ErrorKind::InvalidData, "课表条目无效").into());
            }
            if course.points == 0 {
                course.points = course_points(&course.course_id);
            }
        }
    }
    Ok(app_with_store(courses))
}

fn app_with_store(courses: HashMap<String, Vec<Course>>) -> Router {
    let state = AppState {
        store: Arc::new(Store { courses }),
    };
    Router::new()
        .route("/", get(index))
        .route("/admin", get(admin))
        .route("/healthz", get(health))
        .route("/agent/tools", get(tools))
        .route("/agent/call", post(agent_call))
        .route("/api/schedules/{student_id}", get(native_query))
        .route("/api/teaching/{staff_number}", get(teaching_query))
        .with_state(state)
}

async fn index() -> Html<&'static str> {
    Html(include_str!("../static/index.html"))
}

async fn admin() -> Html<&'static str> {
    Html(include_str!("../static/admin.html"))
}

async fn health() -> Json<Value> {
    Json(json!({"status": "ok", "service": "schedule"}))
}

async fn tools() -> Json<Value> {
    Json(json!({
        "tools": [{
            "name": "schedule_query",
            "description": "按学号查询课表，并可按周次和星期过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {"type": "string", "minLength": 1},
                    "week": {"type": "integer", "minimum": 1, "maximum": 20},
                    "day": {"type": "string", "enum": DAYS}
                },
                "required": ["student_id"],
                "additionalProperties": false
            }
        }]
    }))
}

fn validate_filters(week: Option<u8>, day: Option<&str>) -> Result<(), ApiError> {
    if matches!(week, Some(value) if !(1..=20).contains(&value)) {
        return Err(ApiError::invalid("周次必须是 1 到 20 的整数"));
    }
    if let Some(day) = day {
        if !DAYS.contains(&day) {
            return Err(ApiError::invalid("星期必须是周一至周日"));
        }
    }
    Ok(())
}

fn query_schedule(
    state: &AppState,
    student_id: &str,
    week: Option<u8>,
    day: Option<&str>,
) -> Result<ScheduleResponse, ApiError> {
    let student_id = student_id.trim();
    if student_id.is_empty() {
        return Err(ApiError::invalid("学号不能为空"));
    }
    validate_filters(week, day)?;

    let courses = state
        .store
        .courses
        .get(student_id)
        .ok_or(ApiError::not_found("未找到该学生的课表"))?
        .iter()
        .filter(|course| week.is_none_or(|week| course.weeks.contains(&week)))
        .filter(|course| day.is_none_or(|day| course.day == day))
        .cloned()
        .collect();
    Ok(ScheduleResponse {
        student_id: Some(student_id.to_string()),
        courses,
        periods: &PERIODS,
    })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NativeQuery {
    week: Option<u8>,
    day: Option<String>,
}

async fn native_query(
    State(state): State<AppState>,
    Path(student_id): Path<String>,
    query: Result<Query<NativeQuery>, QueryRejection>,
) -> Result<Json<ScheduleResponse>, ApiError> {
    let Query(query) = query.map_err(|_| ApiError::invalid("查询参数无效"))?;
    query_schedule(&state, &student_id, query.week, query.day.as_deref()).map(Json)
}

async fn teaching_query(
    State(state): State<AppState>,
    Path(staff_number): Path<String>,
    query: Result<Query<NativeQuery>, QueryRejection>,
) -> Result<Json<ScheduleResponse>, ApiError> {
    let Query(query) = query.map_err(|_| ApiError::invalid("查询参数无效"))?;
    let week = query.week;
    let day = query.day.as_deref();
    validate_filters(week, day)?;
    let mut courses: Vec<_> = state
        .store
        .courses
        .values()
        .flatten()
        .filter(|course| course.staff_number.eq_ignore_ascii_case(&staff_number))
        .filter(|course| week.is_none_or(|week| course.weeks.contains(&week)))
        .filter(|course| day.is_none_or(|day| day == course.day))
        .cloned()
        .collect();
    let lesson = |course: &Course| {
        (
            DAYS.iter().position(|day| *day == course.day),
            course.start_period,
            course.end_period,
            course.course_id.clone(),
            course.classroom.clone(),
        )
    };
    courses.sort_by_key(lesson);
    courses.dedup_by_key(|course| lesson(course));
    Ok(Json(ScheduleResponse {
        student_id: None,
        courses,
        periods: &PERIODS,
    }))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AgentCall {
    name: String,
    arguments: Map<String, Value>,
}

async fn agent_call(
    State(state): State<AppState>,
    request: Request,
) -> Result<Json<Value>, ApiError> {
    let bytes = to_bytes(request.into_body(), MAX_BODY_SIZE)
        .await
        .map_err(|_| ApiError::invalid("请求体必须是合法 JSON 且不超过 64KiB"))?;
    let call: AgentCall = serde_json::from_slice(&bytes)
        .map_err(|_| ApiError::invalid("请求必须严格包含 name 和 arguments 字段"))?;
    if call.name != "schedule_query" {
        return Err(ApiError::not_found("未找到指定工具"));
    }
    if call
        .arguments
        .keys()
        .any(|key| !matches!(key.as_str(), "student_id" | "week" | "day"))
    {
        return Err(ApiError::invalid("工具参数包含未知字段"));
    }

    let student_id = call
        .arguments
        .get("student_id")
        .and_then(Value::as_str)
        .ok_or(ApiError::invalid("student_id 必须是非空字符串"))?;
    let week = match call.arguments.get("week") {
        Some(value) => Some(
            value
                .as_u64()
                .filter(|week| (1..=20).contains(week))
                .map(|week| week as u8)
                .ok_or(ApiError::invalid("week 必须是 1 到 20 的整数"))?,
        ),
        None => None,
    };
    let day = match call.arguments.get("day") {
        Some(value) => Some(
            value
                .as_str()
                .ok_or(ApiError::invalid("day 必须是周一至周日"))?,
        ),
        None => None,
    };

    let schedule = query_schedule(&state, student_id, week, day)?;
    let content = serde_json::to_string(&schedule).unwrap();
    Ok(Json(json!({"content": content})))
}
