"""Immutable oracle for delta-green:corrections."""
import service


def main():
    assert service.current_timeout() == 102, service.current_timeout()


if __name__ == "__main__":
    main()
