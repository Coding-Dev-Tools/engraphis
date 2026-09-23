"""Immutable oracle for grove-green:corrections."""
import service


def main():
    assert service.current_timeout() == 138, service.current_timeout()


if __name__ == "__main__":
    main()
