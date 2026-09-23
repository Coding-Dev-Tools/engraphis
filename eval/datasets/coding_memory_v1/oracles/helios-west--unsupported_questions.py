"""Immutable oracle for helios-west:unsupported_questions."""
import service


def main():
    assert service.answer_unsupported('unrecorded fact') is None, service.answer_unsupported('unrecorded fact')


if __name__ == "__main__":
    main()
