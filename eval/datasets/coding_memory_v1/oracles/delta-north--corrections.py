"""Immutable oracle for delta-north:corrections."""
import service


def main():
    assert service.current_timeout() == 94, service.current_timeout()


if __name__ == "__main__":
    main()
