"""Immutable oracle for helios-green:corrections."""
import service


def main():
    assert service.current_timeout() == 150, service.current_timeout()


if __name__ == "__main__":
    main()
