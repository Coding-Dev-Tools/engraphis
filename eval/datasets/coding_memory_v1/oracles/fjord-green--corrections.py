"""Immutable oracle for fjord-green:corrections."""
import service


def main():
    assert service.current_timeout() == 126, service.current_timeout()


if __name__ == "__main__":
    main()
