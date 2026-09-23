"""Immutable oracle for atlas-green:corrections."""
import service


def main():
    assert service.current_timeout() == 66, service.current_timeout()


if __name__ == "__main__":
    main()
