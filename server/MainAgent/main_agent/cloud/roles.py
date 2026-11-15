from enum import StrEnum


class UserRole(StrEnum):
    STUDENT = "student"
    TEACHER = "teacher"


class AcademicTitle(StrEnum):
    TEACHING_ASSISTANT = "助教"
    LECTURER = "讲师"
    ASSOCIATE_PROFESSOR = "副教授"
    PROFESSOR = "教授"
