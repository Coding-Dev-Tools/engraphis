"""Immutable oracle for fjord-north:corrections."""
import service


def main():
    assert service.current_timeout() == 118, service.current_timeout()


if __name__ == "__main__":
    main()
